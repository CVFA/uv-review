#!/usr/bin/env python3
"""
uv-review

A pip-review-like utility that uses `uv pip` instead of `pip`.

Features:
  - Lists outdated packages using `uv pip list --outdated`.
  - Interactive mode opens a Textual TUI when available.
  - Interactive selection is followed by one batched `uv pip install --upgrade ...`.
  - Automatic mode updates packages one-by-one for better failure isolation.
  - Falls back to plain CLI prompts if Textual is unavailable.

Examples:
  uv-review                         # list outdated packages
  uv-review -i                      # TUI selection, then batch update selected packages
  uv-review -i --no-tui             # CLI selection instead of TUI
  uv-review -a                      # automatically update all outdated packages
  uv-review -ar                     # automatic update, then re-check recursively
  uv-review --python .venv/bin/python
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import List, Optional, Set

__version__ = "1.3.0"


@dataclass(frozen=True)
class OutdatedPackage:
    name: str
    current: str
    latest: str


# -----------------------------------------------------------------------------
# Optional Textual TUI support
# -----------------------------------------------------------------------------

TEXTUAL_AVAILABLE = False

try:
    from textual.app import App, ComposeResult
    from textual.containers import Horizontal
    from textual.widgets import Header, Footer, Button, Static, SelectionList
    from textual.widgets.selection_list import Option

    TEXTUAL_AVAILABLE = True
except Exception:
    App = None
    ComposeResult = None
    Horizontal = None
    Header = None
    Footer = None
    Button = None
    Static = None
    SelectionList = None
    Option = None


# -----------------------------------------------------------------------------
# Small helpers
# -----------------------------------------------------------------------------


def cmd_str(cmd: List[str]) -> str:
    try:
        import shlex
        return shlex.join(cmd)
    except Exception:
        return " ".join(cmd)


def verbose_print(args: argparse.Namespace, msg: str) -> None:
    if args.verbose:
        print(msg, file=sys.stderr)


def find_uv(explicit: Optional[str]) -> str:
    candidates: List[str] = []

    if explicit:
        candidates.append(explicit)

    env_uv = os.environ.get("UV_BIN")
    if env_uv:
        candidates.append(env_uv)

    which_uv = shutil.which("uv")
    if which_uv:
        candidates.append(which_uv)

    candidates.append("uv")

    for candidate in candidates:
        if os.path.isabs(candidate):
            if os.path.exists(candidate) and os.access(candidate, os.X_OK):
                return candidate
        else:
            if os.path.exists(candidate) and os.access(candidate, os.X_OK):
                return candidate

            found = shutil.which(candidate)
            if found:
                return found

    return candidates[-1]


def uv_base_args(args: argparse.Namespace) -> List[str]:
    extra: List[str] = []

    if args.python:
        extra.extend(["--python", args.python])

    if args.system:
        extra.append("--system")

    if args.break_system_packages:
        extra.append("--break-system-packages")

    if args.index_url:
        extra.extend(["--index-url", args.index_url])

    for url in args.extra_index_url or []:
        extra.extend(["--extra-index-url", url])

    return extra


def run_uv(
    cmd: List[str],
    args: argparse.Namespace,
    capture: bool = False,
) -> subprocess.CompletedProcess:
    verbose_print(args, f"+ {cmd_str(cmd)}")

    if (
        args.dry_run
        and len(cmd) >= 3
        and cmd[1] == "pip"
        and cmd[2] == "install"
    ):
        print(f"[dry-run] would run: {cmd_str(cmd)}")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    try:
        if capture:
            return subprocess.run(
                cmd,
                check=True,
                text=True,
                capture_output=True,
            )
        return subprocess.run(cmd, check=True, text=True)
    except subprocess.CalledProcessError as exc:
        if capture and exc.stderr:
            verbose_print(args, exc.stderr.strip())
        raise


def run_list_command(cmd: List[str], args: argparse.Namespace) -> subprocess.CompletedProcess:
    try:
        return run_uv(cmd, args, capture=True)
    except subprocess.CalledProcessError:
        if args.exclude_editable and "--exclude-editable" in cmd:
            retry_cmd = [part for part in cmd if part != "--exclude-editable"]
            verbose_print(
                args,
                "Retrying list command without --exclude-editable.",
            )
            return run_uv(retry_cmd, args, capture=True)
        raise


# -----------------------------------------------------------------------------
# Outdated package discovery
# -----------------------------------------------------------------------------


def normalize_payload(payload):
    if isinstance(payload, dict):
        for key in ("packages", "results", "outdated"):
            if key in payload and isinstance(payload[key], list):
                return payload[key]
        return []

    if isinstance(payload, list):
        return payload

    return []


def parse_json_entries(
    entries,
    wanted: Optional[Set[str]],
) -> List[OutdatedPackage]:
    packages: List[OutdatedPackage] = []

    for entry in entries:
        if not isinstance(entry, dict):
            continue

        name = (
            entry.get("name")
            or entry.get("package")
            or entry.get("package_name")
        )

        current = (
            entry.get("version")
            or entry.get("current_version")
            or entry.get("installed_version")
        )

        latest = (
            entry.get("latest_version")
            or entry.get("latest")
            or entry.get("new_version")
        )

        if not name or not latest:
            continue

        if wanted and name.lower() not in wanted:
            continue

        packages.append(
            OutdatedPackage(
                name=str(name),
                current=str(current or "?"),
                latest=str(latest),
            )
        )

    return packages


def get_outdated_json(
    uv: str,
    args: argparse.Namespace,
    wanted: Optional[Set[str]],
) -> List[OutdatedPackage]:
    cmd = [uv, "pip", "list", "--outdated", "--format", "json"]

    if args.exclude_editable:
        cmd.append("--exclude-editable")

    cmd.extend(uv_base_args(args))

    proc = run_list_command(cmd, args)
    stdout = (proc.stdout or "").strip() or "[]"

    low = stdout.lower()
    if "up to date" in low or "no outdated" in low:
        return []

    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "Could not parse JSON from uv.\n"
            f"stdout: {stdout}\n"
            f"stderr: {proc.stderr}"
        ) from exc

    return parse_json_entries(normalize_payload(payload), wanted)


def get_outdated_table(
    uv: str,
    args: argparse.Namespace,
    wanted: Optional[Set[str]],
) -> List[OutdatedPackage]:
    cmd = [uv, "pip", "list", "--outdated"]

    if args.exclude_editable:
        cmd.append("--exclude-editable")

    cmd.extend(uv_base_args(args))

    proc = run_list_command(cmd, args)
    stdout = proc.stdout or ""

    low = stdout.lower()
    if "up to date" in low or "no outdated" in low:
        return []

    packages: List[OutdatedPackage] = []
    lines = [line.rstrip() for line in stdout.splitlines() if line.strip()]

    for line in lines:
        stripped = line.strip()

        if not stripped:
            continue

        if set(stripped) <= {"-", "=", " "}:
            continue

        parts = stripped.split()
        if len(parts) < 3:
            continue

        name, current, latest = parts[0], parts[1], parts[2]

        if (
            name.lower() in {"package", "name"}
            and current.lower() in {"version", "current"}
        ):
            continue

        if wanted and name.lower() not in wanted:
            continue

        packages.append(
            OutdatedPackage(
                name=name,
                current=current,
                latest=latest,
            )
        )

    return packages


def get_outdated(
    uv: str,
    args: argparse.Namespace,
) -> List[OutdatedPackage]:
    wanted = {p.lower() for p in args.packages} if args.packages else None

    try:
        return get_outdated_json(uv, args, wanted)
    except Exception as first_error:
        verbose_print(
            args,
            f"JSON listing failed, falling back to table parsing: {first_error}",
        )

        try:
            return get_outdated_table(uv, args, wanted)
        except Exception as second_error:
            raise RuntimeError(
                "Could not list outdated packages with uv.\n"
                f"JSON error: {first_error}\n"
                f"Table error: {second_error}"
            ) from second_error


# -----------------------------------------------------------------------------
# Display helpers
# -----------------------------------------------------------------------------


def print_outdated(packages: List[OutdatedPackage]) -> None:
    if not packages:
        print("All packages are up to date.")
        return

    packages = sorted(packages, key=lambda p: p.name.lower())

    name_width = max([len("Package")] + [len(p.name) for p in packages])
    current_width = max([len("Version")] + [len(p.current) for p in packages])
    latest_width = max([len("Latest")] + [len(p.latest) for p in packages])

    header = (
        f"{'Package':<{name_width}}  "
        f"{'Version':<{current_width}}  "
        f"{'Latest':<{latest_width}}"
    )

    print(header)
    print("-" * len(header))

    for package in packages:
        print(
            f"{package.name:<{name_width}}  "
            f"{package.current:<{current_width}}  "
            f"{package.latest:<{latest_width}}"
        )


def print_selected(packages: List[OutdatedPackage]) -> None:
    if not packages:
        return

    print("\nSelected packages:")

    for package in packages:
        print(f"  {package.name} {package.current} -> {package.latest}")

    print()


# -----------------------------------------------------------------------------
# Interactive selection: Textual TUI with CLI fallback
# -----------------------------------------------------------------------------


def select_packages_cli(
    packages: List[OutdatedPackage],
) -> Optional[List[OutdatedPackage]]:
    selected: List[OutdatedPackage] = []
    total = len(packages)

    for index, package in enumerate(packages, start=1):
        while True:
            try:
                answer = input(
                    f"[{index}/{total}] Select {package.name} "
                    f"({package.current} -> {package.latest})? "
                    f"[Y/n/a/q] "
                ).strip().lower()
            except EOFError:
                return None

            if answer in {"", "y", "yes"}:
                selected.append(package)
                break

            if answer in {"n", "no"}:
                break

            if answer in {"a", "all"}:
                selected.extend(packages[index - 1 :])
                return selected

            if answer in {"q", "quit", "abort"}:
                return None

            print("Please answer y, n, a (all remaining), or q.")

    return selected


if TEXTUAL_AVAILABLE:

    class PackageSelector(App):
        TITLE = "uv-review"

        CSS = """
        #description {
            margin: 1 1 0 1;
        }

        #packages {
            margin: 1;
            height: 1fr;
        }

        #buttons {
            dock: bottom;
            margin: 1;
            align-horizontal: center;
        }

        Button {
            margin: 0 1;
        }
        """

        BINDINGS = [
            ("u", "update", "Update selected"),
            ("enter", "update", "Update selected"),
            ("escape", "cancel", "Cancel"),
            ("q", "cancel", "Cancel"),
        ]

        def __init__(self, packages: List[OutdatedPackage]):
            super().__init__()
            self.packages = packages

        def compose(self) -> "ComposeResult":
            yield Header()

            yield Static(
                "Select packages to update.\n"
                "Space toggles, Enter updates, Esc cancels.",
                id="description",
            )

            options = [
                Option(
                    f"{package.name}  {package.current} -> {package.latest}",
                    package.name,
                    True,
                )
                for package in self.packages
            ]

            yield SelectionList(*options, id="packages")

            with Horizontal(id="buttons"):
                yield Button("Update selected", id="update", variant="success")
                yield Button("Cancel", id="cancel", variant="error")

            yield Footer()

        def action_update(self) -> None:
            selection_list = self.query_one(SelectionList)
            self.exit(list(selection_list.selected))

        def action_cancel(self) -> None:
            self.exit(None)

        def on_button_pressed(self, event) -> None:
            if event.button.id == "update":
                self.action_update()
            elif event.button.id == "cancel":
                self.action_cancel()

        def on_mount(self) -> None:
            self.query_one(SelectionList).focus()


def select_packages_tui(
    packages: List[OutdatedPackage],
) -> Optional[List[OutdatedPackage]]:
    if not packages:
        return []

    if not TEXTUAL_AVAILABLE:
        return select_packages_cli(packages)

    app = PackageSelector(packages)
    selected_values = app.run()

    if selected_values is None:
        return None

    selected_names = set(selected_values)

    return [
        package
        for package in packages
        if package.name in selected_names
    ]


def select_packages(
    packages: List[OutdatedPackage],
    use_tui: bool = True,
) -> Optional[List[OutdatedPackage]]:
    if os.environ.get("UV_REVIEW_NO_TUI", "").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }:
        use_tui = False

    if (
        use_tui
        and TEXTUAL_AVAILABLE
        and sys.stdin.isatty()
        and sys.stdout.isatty()
    ):
        try:
            return select_packages_tui(packages)
        except Exception as exc:
            print(
                f"TUI failed, falling back to CLI prompts: {exc}",
                file=sys.stderr,
            )

    elif (
        use_tui
        and not TEXTUAL_AVAILABLE
        and sys.stdin.isatty()
        and sys.stdout.isatty()
    ):
        print(
            "TUI disabled because 'textual' is not installed.\n"
            "Install it with: uv pip install textual",
            file=sys.stderr,
        )

    return select_packages_cli(packages)


# -----------------------------------------------------------------------------
# Update helpers
# -----------------------------------------------------------------------------


def update_package(
    uv: str,
    args: argparse.Namespace,
    package: OutdatedPackage,
) -> bool:
    if args.pin_latest:
        spec = f"{package.name}=={package.latest}"
    else:
        spec = package.name

    cmd = [uv, "pip", "install", "--upgrade", spec]
    cmd.extend(uv_base_args(args))

    try:
        run_uv(cmd, args)
        return True
    except subprocess.CalledProcessError as exc:
        print(
            f"Failed to update {package.name}: exit code {exc.returncode}",
            file=sys.stderr,
        )

        if exc.stderr:
            print(exc.stderr.strip(), file=sys.stderr)

        return False
    except OSError as exc:
        print(f"Failed to run uv: {exc}", file=sys.stderr)
        return False


def update_packages_batch(
    uv: str,
    args: argparse.Namespace,
    packages: List[OutdatedPackage],
) -> bool:
    if not packages:
        return True

    specs: List[str] = []

    for package in packages:
        if args.pin_latest:
            specs.append(f"{package.name}=={package.latest}")
        else:
            specs.append(package.name)

    cmd = [uv, "pip", "install", "--upgrade", *specs]
    cmd.extend(uv_base_args(args))

    try:
        run_uv(cmd, args)
        return True
    except subprocess.CalledProcessError as exc:
        print(
            "Failed to update selected packages: "
            f"exit code {exc.returncode}",
            file=sys.stderr,
        )

        if exc.stderr:
            print(exc.stderr.strip(), file=sys.stderr)

        return False
    except OSError as exc:
        print(f"Failed to run uv: {exc}", file=sys.stderr)
        return False


# -----------------------------------------------------------------------------
# CLI definition
# -----------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="uv-review",
        description=(
            "Review and update outdated Python packages using uv pip. "
            "Interactive mode selects packages first, then updates all "
            "selected packages in one command."
        ),
    )

    parser.add_argument(
        "-a",
        "--auto",
        action="store_true",
        help="automatically update all outdated packages",
    )

    parser.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="alias for --auto",
    )

    parser.add_argument(
        "-i",
        "--interactive",
        action="store_true",
        help=(
            "interactively select outdated packages, then update all selected "
            "packages in one uv command"
        ),
    )

    parser.add_argument(
        "-r",
        "--recursive",
        action="store_true",
        help=(
            "after updating, check again and continue until no outdated "
            "packages remain or no further progress is made"
        ),
    )

    parser.add_argument(
        "-l",
        "--local",
        action="store_true",
        help=(
            "accepted for pip-review compatibility; uv environment selection "
            "is controlled by --python, --system, or the active environment"
        ),
    )

    parser.add_argument(
        "--user",
        action="store_true",
        help=(
            "accepted for pip-review compatibility; uv does not have an exact "
            "equivalent of pip --user"
        ),
    )

    parser.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="list outdated packages and show what would be done, but do not install",
    )

    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="print uv commands and extra diagnostics",
    )

    parser.add_argument(
        "--uv-bin",
        default=None,
        metavar="UV",
        help="path to the uv executable",
    )

    parser.add_argument(
        "--python",
        default=None,
        metavar="PYTHON",
        help="Python interpreter or environment for uv to operate on",
    )

    parser.add_argument(
        "--system",
        action="store_true",
        help="use the system Python environment with uv",
    )

    parser.add_argument(
        "--break-system-packages",
        action="store_true",
        help="allow uv to modify system packages, where supported",
    )

    parser.add_argument(
        "--index-url",
        default=None,
        metavar="URL",
        help="package index URL passed to uv",
    )

    parser.add_argument(
        "--extra-index-url",
        action="append",
        default=[],
        metavar="URL",
        help="additional package index URL passed to uv; may be repeated",
    )

    parser.add_argument(
        "--exclude-editable",
        action="store_true",
        help="exclude editable installations from the outdated list, where supported",
    )

    parser.add_argument(
        "--pin-latest",
        action="store_true",
        help="install name==latest_version instead of just name",
    )

    parser.add_argument(
        "--fail-on-outdated",
        action="store_true",
        help=(
            "exit with status 1 when outdated packages are found in "
            "review-only or dry-run mode"
        ),
    )

    parser.add_argument(
        "--no-tui",
        action="store_true",
        help="use plain command-line prompts instead of the Textual TUI",
    )

    parser.add_argument(
        "-V",
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )

    parser.add_argument(
        "packages",
        nargs="*",
        help="only review or update these packages",
    )

    return parser


# -----------------------------------------------------------------------------
# Main entry point
# -----------------------------------------------------------------------------


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.yes:
        args.auto = True

    if args.auto and args.interactive:
        print(
            "error: --auto/--yes and --interactive are mutually exclusive",
            file=sys.stderr,
        )
        return 2

    if args.local:
        verbose_print(
            args,
            "note: --local is accepted for compatibility; "
            "uv environment selection uses --python, --system, "
            "or the active environment.",
        )

    if args.user:
        verbose_print(
            args,
            "note: --user is accepted for compatibility; "
            "uv does not have an exact equivalent of pip --user.",
        )

    uv = find_uv(args.uv_bin)

    if not shutil.which(uv) and not os.path.exists(uv):
        print(
            "error: uv executable not found. Install uv or set UV_BIN / --uv-bin.",
            file=sys.stderr,
        )
        return 127

    exit_code = 0
    pass_number = 0
    previous_snapshot = None

    while True:
        pass_number += 1

        try:
            outdated = get_outdated(uv, args)
        except subprocess.CalledProcessError as exc:
            print("Error: failed to list outdated packages.", file=sys.stderr)
            if exc.stderr:
                print(exc.stderr.strip(), file=sys.stderr)
            return 1
        except RuntimeError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1

        if not outdated:
            if pass_number == 1:
                print("All packages are up to date.")
            else:
                print("No more outdated packages.")
            return exit_code

        outdated = sorted(outdated, key=lambda p: p.name.lower())

        print_outdated(outdated)

        if args.dry_run:
            if args.fail_on_outdated:
                return 1
            return 0

        if not args.auto and not args.interactive:
            if args.fail_on_outdated:
                return 1
            return exit_code

        snapshot = frozenset(
            (p.name.lower(), p.current, p.latest) for p in outdated
        )

        if (
            args.recursive
            and previous_snapshot is not None
            and snapshot == previous_snapshot
        ):
            print(
                "Recursive update made no further progress.",
                file=sys.stderr,
            )
            return exit_code

        updated = 0
        failed = 0
        skipped = 0

        if args.interactive:
            selected = select_packages(outdated, use_tui=not args.no_tui)

            if selected is None:
                print("Aborted.", file=sys.stderr)
                return 130

            skipped = len(outdated) - len(selected)

            if not selected:
                print("No packages selected.")
                return exit_code

            print_selected(selected)

            if update_packages_batch(uv, args, selected):
                updated = len(selected)
            else:
                failed = len(selected)
                exit_code = 1

        else:
            for package in outdated:
                if update_package(uv, args, package):
                    updated += 1
                else:
                    failed += 1
                    exit_code = 1

        if failed:
            print(f"{failed} package update(s) failed.", file=sys.stderr)

        if skipped:
            verbose_print(args, f"{skipped} package update(s) skipped.")

        if not args.recursive:
            return exit_code

        if updated == 0:
            return exit_code

        previous_snapshot = snapshot


if __name__ == "__main__":
    sys.exit(main())
