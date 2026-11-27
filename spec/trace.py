#!/usr/bin/env python3
"""Print a TLC log as a compact counterexample: action name and changed fields.

    python3 -I spec/trace.py spec/states/logs/<run>.log
"""
import re
import sys

RECORD_FIELD = re.compile(r'(\w+) \|-> (\{[^}]*\}|"[^"]*"|\w+)')


def flatten(block):
    out = {}
    for part in re.split(r"\n?/\\ (?=\w+ = )", block.split("\n\n")[0]):
        name, sep, value = part.strip().partition(" = ")
        if not sep:
            continue
        value = " ".join(value.split())
        if name not in ("iss", "pr", "wk"):
            out[name] = value
            continue
        keys = re.findall(r"(\w+) :> \[", value)
        records = [r for r in re.split(r"\[", value)[1:] if "|->" in r]
        for index, record in enumerate(records):
            key = keys[index] if index < len(keys) else str(index + 1)
            for field, item in RECORD_FIELD.findall(record):
                out[f"{name}[{key}].{field}"] = item
    return out


def main(path):
    text = open(path, encoding="utf-8").read()
    start = re.search(r"is violated|properties were violated", text)
    if not start:
        sys.exit("no violation in this log")
    pieces = re.split(r"\nState (\d+): ?(.*)\n", text[start.start():])
    previous = {}
    for number, action, body in zip(pieces[1::3], pieces[2::3], pieces[3::3]):
        action = re.sub(r" line.*", "", action).strip("<>")
        state = flatten(body)
        if not state:
            print(number, action)
            continue
        if previous:
            changed = {k: v for k, v in state.items() if previous.get(k) != v}
        else:
            changed = {k: v for k, v in state.items() if k == "bud"}
        print(number, action, "  ".join(f"{k}={v}" for k, v in changed.items()))
        previous = state
    for line in text.splitlines():
        if "Back to state" in line or "Stuttering" in line:
            print(line)


if __name__ == "__main__":
    main(sys.argv[1])
