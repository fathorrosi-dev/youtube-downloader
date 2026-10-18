"""Run the test suite in CI and say which tests failed, instead of only "exit code 1".

    python scripts/ci_summary.py [--title NAME] -- <test command ...>

The command's output is shown live and also kept. When it fails, every `FAIL:` / `ERROR:` line
of unittest becomes a `::error::` annotation (visible on the run page without opening the log) and
the same list, with the last lines of the output, is written to the job summary
($GITHUB_STEP_SUMMARY). Standard library only, so it behaves the same on Linux, macOS and Windows.
The exit code of the command is kept.
"""

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

FAILURE = re.compile(r"^(FAIL|ERROR): (.+)$")
TAIL = 60


def failures(lines):
    """('FAIL', 'test_x (tests.test_y.Z.test_x)') for each failed test in a unittest log."""
    found = []
    for line in lines:
        match = FAILURE.match(line.rstrip())
        if match and match[2] not in [name for _, name in found]:
            found.append((match[1], match[2]))
    return found


def annotation(kind, name, title):
    text = f"{kind}: {name}".replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    return f"::error title={title}::{text}"


def summary(title, code, found, lines):
    out = [f"### {title}", ""]
    if not code:
        return "\n".join([*out, "All tests passed.", ""])
    out.append(
        f"Exit code {code}. {len(found)} failing test(s):" if found else f"Exit code {code}."
    )
    out.append("")
    out += [f"- `{kind}` {name}" for kind, name in found]
    out += ["", "<details><summary>Last lines of the output</summary>", "", "```"]
    out += [line.rstrip() for line in lines[-TAIL:]]
    out += ["```", "", "</details>", ""]
    return "\n".join(out)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--title", default="Tests")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("give the test command after --")
    lines = []
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
        code = process.wait()
    found = failures(lines)
    if code:
        for kind, name in found:
            print(annotation(kind, name, args.title))
        if not found:
            print(f"::error title={args.title}::the command exited with code {code}")
    target = os.environ.get("GITHUB_STEP_SUMMARY")
    if target:
        with Path(target).open("a", encoding="utf-8") as stream:
            stream.write(summary(args.title, code, found, lines))
    return code


if __name__ == "__main__":
    sys.exit(main())
