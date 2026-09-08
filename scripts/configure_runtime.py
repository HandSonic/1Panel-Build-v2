#!/usr/bin/env python3
"""Set embedded runtime defaults without depending on a fixed installer line."""
import argparse
import os
from pathlib import Path
import re


def resolve_channel(version, channel=""):
    if channel:
        if channel not in {"stable", "beta", "dev"}:
            raise ValueError("CHANNEL must be stable, beta or dev")
        return channel
    if re.fullmatch(r"v?\d+\.\d+\.\d+(?:-lts)?", version):
        return "stable"
    if re.search(r"(?:^|[-.])(beta|alpha|rc|pre)(?:[-.\d]|$)", version, re.I):
        return "beta"
    return "dev"


def set_base_defaults(text, channel, version):
    """Only edit direct scalar children of base; preserve comments and other YAML."""
    lines = text.splitlines(keepends=True)
    for start, line in enumerate(lines):
        if not re.match(r"^base\s*:\s*(?:#.*)?$", line.rstrip()):
            continue
        end = start + 1
        while end < len(lines):
            if lines[end].strip() and not lines[end].startswith((" ", "\t", "#")):
                break
            end += 1
        child_indents = [len(x) - len(x.lstrip()) for x in lines[start + 1:end]
                         if x.strip() and not x.lstrip().startswith("#")]
        indent = min(child_indents) if child_indents else 2
        found_mode = False
        for index in range(start + 1, end):
            match = re.match(r"^(\s+)(mode|version)\s*:\s*([^#\r\n]*)(.*)$", lines[index].rstrip("\r\n"))
            if match and len(match[1]) == indent:
                value = channel if match[2] == "mode" else version
                comment = (" " + match[4]) if match[4] else ""
                lines[index] = f"{match[1]}{match[2]}: {value}{comment}\n"
                found_mode |= match[2] == "mode"
        if not found_mode:
            lines.insert(start + 1, " " * indent + f"mode: {channel}\n")
        return "".join(lines)
    return None


def configure(root, version, channel=""):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", version):
        raise ValueError("VERSION must be a tag or simple branch name")
    channel = resolve_channel(version, channel)
    for module in ("core", "agent"):
        module_root = root / module
        # Locate the config by its contents, tolerating file/path renames upstream.
        configs = sorted(set(module_root.rglob("*.yaml")) | set(module_root.rglob("*.yml")))
        matched = []
        for config in configs:
            relative_parts = config.relative_to(module_root).parts
            if any(part in {"vendor", "testdata", "node_modules", ".git"} for part in relative_parts):
                continue
            content = config.read_text(encoding="utf-8")
            # Prefer embedded app config, not compose/workflow or unrelated YAML.
            if "conf" not in relative_parts and "config" not in relative_parts:
                continue
            updated = set_base_defaults(content, channel, version)
            if updated is not None:
                config.write_text(updated, encoding="utf-8")
                matched.append(str(config.relative_to(root)))
        if not matched:
            raise ValueError(f"No base runtime config found for {module}; refusing to ship an unknown update channel")
        print(f"{module}: channel={channel}, configs={', '.join(matched)}")
    ctl = root / "1pctl"
    content = ctl.read_text(encoding="utf-8")
    updated, count = re.subn(r"(?m)^(\s*(?:export\s+)?ORIGINAL_VERSION\s*=).*$",
                             lambda match: match[1] + version, content)
    if not count:
        raise ValueError("1pctl no longer defines ORIGINAL_VERSION; cannot set installed version")
    ctl.write_text(updated, encoding="utf-8")
    return channel


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("version")
    parser.add_argument("--channel", default=os.environ.get("CHANNEL", ""))
    args = parser.parse_args()
    try:
        configure(args.root, args.version, args.channel)
    except (OSError, ValueError) as error:
        parser.exit(1, f"Runtime configuration: {error}\n")
