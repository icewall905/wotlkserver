#!/usr/bin/env python3
"""Rebuild a live AzerothCore .conf from a newer .conf.dist, keeping current values.

Usage: merge-conf.py <new.conf.dist> <live.conf> [--write]

Every key in the new .dist keeps its documentation; if the live config sets that
key, the live value is used. Keys only present in the live config are listed as
obsolete (and appended in a marked block so nothing is silently lost).
Without --write it only prints a summary.
"""
import re
import sys

KEY_RE = re.compile(r'^([A-Za-z][A-Za-z0-9_.]*)\s*=\s*(.*?)\s*$')


def read(path):
    with open(path, encoding='utf-8', errors='surrogateescape', newline='') as f:
        return f.read().splitlines()


def values(lines):
    out = {}
    for line in lines:
        m = KEY_RE.match(line)
        if m:
            out[m.group(1)] = m.group(2)
    return out


def main():
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    dist_path, live_path = sys.argv[1], sys.argv[2]
    write = '--write' in sys.argv[3:]

    dist = read(dist_path)
    live_vals = values(read(live_path))
    dist_keys = set(values(dist))

    merged, kept, added = [], 0, []
    for line in dist:
        m = KEY_RE.match(line)
        if m and m.group(1) in live_vals:
            key = m.group(1)
            merged.append(f'{key} = {live_vals[key]}')
            kept += 1
        else:
            if m:
                added.append(m.group(1))
            merged.append(line)

    obsolete = [k for k in live_vals if k not in dist_keys]
    if obsolete:
        merged += ['', '#' * 40, '# Obsolete keys carried over from the previous config', '#' * 40]
        merged += [f'# {k} = {live_vals[k]}' for k in obsolete]

    print(f'{live_path}: kept={kept} new={len(added)} obsolete={len(obsolete)}'
          + (f' ({", ".join(obsolete)})' if obsolete else ''))

    if write:
        with open(live_path, 'w', encoding='utf-8', errors='surrogateescape', newline='') as f:
            f.write('\n'.join(merged) + '\n')


if __name__ == '__main__':
    main()
