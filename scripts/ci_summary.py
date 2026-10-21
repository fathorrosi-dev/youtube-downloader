"""Run the test suite in CI and say which tests failed, instead of only "exit code 1".

    python scripts/ci_summary.py [--title NAME] -- <test command ...>

The command's output is shown live and also kept. When it fails, every `FAIL:` / `ERROR:` line
of unittest becomes a `::error::` annotation (visible on the run page without opening the log) and
the same list, with the last lines of the output, is written to the job summary
($GITHUB_STEP_SUMMARY). The ten slowest tests are named there too, pass or fail. Standard library
only, so it behaves the same on Linux, macOS and Windows. The exit code of the command is kept.

A test is timed from the previous result line of `unittest -v` to its own, so the time includes
what the test printed and, for the first test of a class, the class's setUp; the very first test
of the run is left out. This is measured here, not by the runner, because `unittest --durations`
exists only from Python 3.12 and the jobs also run 3.11.
"""

import argparse
import os
import re
import subprocess
import sys
import time
from pathlib import Path

FAILURE = re.compile(r"^(FAIL|ERROR): (.+)$")
# "test_x (tests.test_y.Z.test_x) ... ok", also with "skipped 'why'" or "expected failure".
RESULT = re.compile(
    r"^(?P<name>\S.*?) \.\.\. (?:ok|FAIL|ERROR|skipped\b.*|expected failure|unexpected success)$"
)
TAIL = 60
SLOWEST = 10


def failures(lines):
    """('FAIL', 'test_x (tests.test_y.Z.test_x)') for each failed test in a unittest log."""
    found = []
    for line in lines:
        match = FAILURE.match(line.rstrip())
        if match and match[2] not in [name for _, name in found]:
            found.append((match[1], match[2]))
    return found


def result_name(line):
    """The test a `unittest -v` result line is about, or None for any other line."""
    match = RESULT.match(line.rstrip())
    return match["name"] if match else None


def durations(stamped):
    """[(name, seconds)] from [(name, clock)], the result lines in the order they came.

    Tests run one after the other, so a test lasted from the line before it to its own."""
    timed, before = [], None
    for name, clock in stamped:
        if before is not None:
            timed.append((name, clock - before))
        before = clock
    return timed


def slowest(timed, count=SLOWEST):
    """The `count` longest of [(name, seconds)], longest first; equal times keep their order."""
    return sorted(timed, key=lambda pair: -pair[1])[:count]


def slow_lines(timed):
    return [f"{seconds:7.2f} s  {name}" for name, seconds in slowest(timed)]


def annotation(kind, name, title):
    text = f"{kind}: {name}".replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    return f"::error title={title}::{text}"


def summary(title, code, found, lines, timed=()):
    out = [f"### {title}", ""]
    if not code:
        out += ["All tests passed.", ""]
    else:
        out.append(
            f"Exit code {code}. {len(found)} failing test(s):" if found else f"Exit code {code}."
        )
        out.append("")
        out += [f"- `{kind}` {name}" for kind, name in found]
        out += ["", "<details><summary>Last lines of the output</summary>", "", "```"]
        out += [line.rstrip() for line in lines[-TAIL:]]
        out += ["```", "", "</details>", ""]
    if timed:
        out += [f"<details><summary>Slowest {min(SLOWEST, len(timed))} tests</summary>", ""]
        out += ["```", *slow_lines(timed), "```", "", "</details>", ""]
    return "\n".join(out)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--title", default="Tests")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("give the test command after --")
    lines, stamped = [], []
    with subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    ) as process:
        for line in process.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
            lines.append(line)
            name = result_name(line)
            if name:
                stamped.append((name, time.monotonic()))
        code = process.wait()
    found = failures(lines)
    timed = durations(stamped)
    if timed:
        print(f"\nSlowest {min(SLOWEST, len(timed))} tests, from one result line to the next:")
        print("\n".join(slow_lines(timed)))
    if code:
        for kind, name in found:
            print(annotation(kind, name, args.title))
        if not found:
            print(f"::error title={args.title}::the command exited with code {code}")
    target = os.environ.get("GITHUB_STEP_SUMMARY")
    if target:
        with Path(target).open("a", encoding="utf-8") as stream:
            stream.write(summary(args.title, code, found, lines, timed))
    return code


if __name__ == "__main__":
    sys.exit(main())
