# numdb_diff.py - semantic diff of numdb data between source and installed package
#
# Copyright (C) 2026 Arthur de Jong
#
# This library is free software; you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public
# License as published by the Free Software Foundation; either
# version 2.1 of the License, or (at your option) any later version.
#
# This library is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public
# License along with this library; if not, see <https://www.gnu.org/licenses/>.

"""Compare numdb number data between the source tree and an installed package.

Before a release this answers the question: if the wheel is missing (part of)
a numdb data file, which numbers would be split or labelled differently by
the installed package compared to the source data?

The number space of a fixed-length numdb is far too large to enumerate (e.g.
12-digit ISBN bookland lookups), so the behaviour of each database is
computed as a partition of the whole fixed-length number space into
intervals on which the split and the properties are constant.  The two
partitions (source and installed) are then merged and only the intervals
where they disagree are reported, each with a replayable witness number.

Findings are reported in separate categories:

* missing resources: the installed package cannot load the .dat file at all
* shrunk valid ranges: numbers the source splits into more parts than the
  installed package (and the reverse: expanded)
* changed: both sides split but the split or the properties differ
* shadowed text differences: lines that differ textually but are fully
  shadowed by other (shorter) rules, so they cannot change any lookup

The installed package is queried in an isolated subprocess that has the
source directory removed from sys.path, so the installed package cannot
borrow data from the source tree.

Usage:

    python scripts/numdb_diff.py compare \
        --source-dir stdnum --install-dir /path/to/install \
        --db isbn:12 --db imsi:15 [--json report.json]

    python scripts/numdb_diff.py dump --db isbn  # used internally
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

# the alphabet of the number space (all release numdb files are digits)
ALPHABET = '0123456789'

# same expressions as stdnum.numdb so parsing is identical
_line_re = re.compile(
    r'^(?P<indent> *)'
    r'(?P<ranges>([^-,\s]+(-[^-,\s]+)?)(,[^-,\s]+(-[^-,\s]+)?)*)\s*'
    r'(?P<props>.*)$')
_prop_re = re.compile(
    r'(?P<prop>[0-9a-zA-Z-_]+)="(?P<value>[^"]*)"')


# Parsing (mirrors stdnum.numdb.read, keeps source line numbers)
# Entries are [length, low, high, props, children, lines] where lines is a
# tuple of 1-based source line numbers the entry originates from.

def parse_dat(text):
    """Parse numdb data to an entry tree, mirroring stdnum.numdb.read."""
    root = []
    stack = {0: root}
    last_indent = 0
    for lineno, line in enumerate(text.splitlines(), 1):
        if not line.strip() or line[0] == '#':
            continue
        match = _line_re.search(line)
        assert match is not None, f'line {lineno} does not parse: {line!r}'
        indent = len(match.group('indent'))
        props = dict(_prop_re.findall(match.group('props')))
        if indent > last_indent:
            # children attach to the last entry of the previous level
            stack[indent] = stack[last_indent][-1][4]
        # all ranges of one line share a single children list, exactly like
        # stdnum.numdb.read, so deeper lines attach to every range of the line
        children = []
        for rnge in match.group('ranges').split(','):
            if '-' in rnge:
                low, high = rnge.split('-')
            else:
                low = high = rnge
            assert len(low) == len(high), f'line {lineno}: mixed range width'
            stack[indent].append(
                [len(low), low, high, props, children, (lineno,)])
        last_indent = indent
    return root


def to_numdb(tree):
    """Convert a parsed entry tree to stdnum.numdb prefix tuples."""
    return [
        (length, low, high, props, to_numdb(children))
        for length, low, high, props, children, _lines in tree]


def normalize5(tree):
    """Convert JSON-ish prefix lists to stdnum.numdb prefix tuples."""
    return [
        (length, low, high, props, normalize5(children))
        for length, low, high, props, children in (
            tuple(entry[:5]) for entry in tree)]


def dat_line_records(text):
    """Return (lineno, indent, ranges, props) for each data line."""
    records = []
    for lineno, line in enumerate(text.splitlines(), 1):
        if not line.strip() or line[0] == '#':
            continue
        match = _line_re.search(line)
        assert match is not None, f'line {lineno} does not parse: {line!r}'
        records.append((
            lineno, len(match.group('indent')), match.group('ranges'),
            tuple(sorted(_prop_re.findall(match.group('props')))),
            line.strip()))
    return records


# Fixed-length string arithmetic over the alphabet

def _succ(s, alpha):
    """Return the next string of the same length, or None if s is the last."""
    chars = list(s)
    for i in range(len(chars) - 1, -1, -1):
        j = alpha.index(chars[i])
        if j < len(alpha) - 1:
            chars[i] = alpha[j + 1]
            return ''.join(chars)
        chars[i] = alpha[0]
    return None


def _pred(s, alpha):
    """Return the previous string of the same length, or None."""
    chars = list(s)
    for i in range(len(chars) - 1, -1, -1):
        j = alpha.index(chars[i])
        if j > 0:
            chars[i] = alpha[j - 1]
            return ''.join(chars)
        chars[i] = alpha[-1]
    return None


def split_by_prefix(a, b, length, alpha):
    """Split interval [a, b] into sub-intervals with a constant prefix of
    the given length. Yields (lo, hi) pairs in ascending order."""
    hi_c = alpha[-1]
    m = len(a)
    while a <= b:
        end = min(a[:length] + hi_c * (m - length), b)
        yield a, end
        nxt = _succ(end, alpha)
        if nxt is None:
            return
        a = nxt


# Behaviour regions: partition of the fixed-length number space into
# intervals on which split() and the per-part properties are constant.
# A region node is ('unmatched',) or
# ('match', part_length, props_tuple, child_regions_or_None, line_numbers).

def compute_regions(entries, m, alpha=ALPHABET):
    """Partition the space of m-character strings into behaviour regions."""
    lo_c, hi_c = alpha[0], alpha[-1]
    space_lo, space_hi = lo_c * m, hi_c * m
    # project each entry to the interval of m-strings whose prefix matches
    projected = []
    for order, entry in enumerate(entries):
        length = entry[0]
        if length <= m:
            projected.append((
                entry[1] + lo_c * (m - length),
                entry[2] + hi_c * (m - length),
                order, entry))
    cuts = {space_lo}
    for plo, phi, _order, _entry in projected:
        cuts.add(plo)
        nxt = _succ(phi, alpha)
        if nxt is not None:
            cuts.add(nxt)
    cuts = sorted(cuts)
    regions = []
    for i, a in enumerate(cuts):
        b = _pred(cuts[i + 1], alpha) if i + 1 < len(cuts) else space_hi
        covering = sorted(
            (order, entry) for plo, phi, order, entry in projected
            if plo <= a <= phi)
        if not covering:
            node = ('unmatched',)
        else:
            lmin = min(entry[0] for _order, entry in covering)
            props, children, lines = {}, [], set()
            for _order, entry in covering:
                if entry[0] == lmin:
                    props.update(entry[3])
                    children.extend(entry[4])
                    lines.update(entry[5] if len(entry) > 5 else ())
            child_regions = (
                compute_regions(children, m - lmin, alpha)
                if children and lmin < m else None)
            node = (
                'match', lmin, tuple(sorted(props.items())),
                child_regions, tuple(sorted(lines)))
        if regions and regions[-1][2] == node and \
                _succ(regions[-1][1], alpha) == a:
            regions[-1][1] = b
        else:
            regions.append([a, b, node])
    return tuple((a, b, node) for a, b, node in regions)


def flatten(regions, m, alpha=ALPHABET, path=(), lines=()):
    """Flatten nested regions to (lo, hi, label, lines) over m-strings.

    The label is a tuple of ('part', length, props) segments followed by a
    final ('rest',) segment for the unmatched remainder, mirroring what
    stdnum.numdb.NumDB.info() returns for any number in the interval.
    """
    out = []
    for a, b, node in regions:
        if node[0] == 'unmatched':
            out.append((a, b, path + (('rest',),), lines))
            continue
        _, lmin, props, children, nlines = node
        npath = path + (('part', lmin, props),)
        nlines = lines + nlines
        if not children:
            out.append((
                a, b, npath + (('rest',),) if lmin < m else npath, nlines))
            continue
        child_flat = flatten(children, m - lmin, alpha, npath, nlines)
        for plo, phi in split_by_prefix(a, b, lmin, alpha):
            prefix = plo[:lmin]
            slo, shi = plo[lmin:], phi[lmin:]
            for clo, chi, clabel, clines in child_flat:
                lo2, hi2 = max(slo, clo), min(shi, chi)
                if lo2 <= hi2:
                    out.append((prefix + lo2, prefix + hi2, clabel, clines))
    return out


def diff(flat_a, flat_b, alpha=ALPHABET):
    """Return (lo, hi, label_a, label_b) intervals where the two flat
    partitions disagree. Both must partition the same space."""
    diffs = []
    i = j = 0
    cur = None
    while i < len(flat_a) and j < len(flat_b):
        a_lo, a_hi, la, _ = flat_a[i]
        b_lo, b_hi, lb, _ = flat_b[j]
        lo = max(a_lo, b_lo)
        assert cur is None or cur == lo, f'partitions out of sync at {lo}'
        hi = min(a_hi, b_hi)
        if la != lb:
            if diffs and diffs[-1][2] == la and diffs[-1][3] == lb and \
                    _succ(diffs[-1][1], alpha) == lo:
                diffs[-1][1] = hi
            else:
                diffs.append([lo, hi, la, lb])
        if a_hi == hi:
            i += 1
        if b_hi == hi:
            j += 1
        cur = _succ(hi, alpha)
    assert i == len(flat_a) and j == len(flat_b), 'partition length mismatch'
    return [tuple(d) for d in diffs]


def classify(source_label, installed_label):
    """Classify a differing interval as shrunk, expanded or changed."""
    pa = [s for s in source_label if s[0] == 'part']
    pb = [s for s in installed_label if s[0] == 'part']
    if len(pb) < len(pa) and pa[:len(pb)] == pb:
        return 'shrunk'
    if len(pa) < len(pb) and pb[:len(pa)] == pa:
        return 'expanded'
    return 'changed'


def label_info(label, number):
    """Reconstruct the NumDB.info() result for a number from its label."""
    out = []
    pos = 0
    for seg in label:
        if seg[0] == 'part':
            _kind, length, props = seg
            out.append((number[pos:pos + length], dict(props)))
            pos += length
        elif pos < len(number):
            out.append((number[pos:], {}))
            pos = len(number)
    return out


def provenance(flat):
    """Return the set of source line numbers contributing to any label."""
    lines = set()
    for _lo, _hi, _label, flines in flat:
        lines.update(flines)
    return lines


# Isolated dumping of the installed package

def dump_installed(db_names, expect_prefix, forbid_prefix):
    """Load databases from the installed stdnum package and print JSON.

    This runs in an isolated subprocess without the source tree on sys.path
    so the installed package cannot borrow data from the source directory.
    """
    import stdnum
    import stdnum.numdb
    stdnum_file = Path(stdnum.__file__).resolve()
    expect = Path(expect_prefix).resolve()
    forbid = Path(forbid_prefix).resolve()
    if not stdnum_file.is_relative_to(expect):
        raise SystemExit(
            f'installed stdnum resolved to {stdnum_file} which is not '
            f'inside the expected install directory {expect}')
    if stdnum_file.is_relative_to(forbid):
        raise SystemExit(
            f'installed stdnum resolved to {stdnum_file} which is inside '
            f'the source directory {forbid}; refusing to borrow source data')
    result = {
        'stdnum_file': str(stdnum_file),
        'stdnum_version': getattr(stdnum, '__version__', None),
        'dbs': {},
    }
    for name in db_names:
        entry = {}
        try:
            db = stdnum.numdb.get(name)
            entry['prefixes'] = db.prefixes
        except Exception as e:
            entry['error'] = f'{type(e).__name__}: {e}'
        try:
            with stdnum.numdb._get_resource_stream(name + '.dat') as fp:
                entry['text'] = fp.read().decode('utf-8')
        except Exception as e:
            entry['text_error'] = f'{type(e).__name__}: {e}'
        result['dbs'][name] = entry
    json.dump(result, sys.stdout)


def run_dump(db_names, install_dir, source_root):
    """Run the dump in an isolated subprocess bound to the install dir."""
    cmd = [sys.executable, '-s']
    if sys.version_info >= (3, 11):
        cmd.append('-P')  # do not prepend the script directory to sys.path
    cmd += [str(Path(__file__).resolve()), 'dump']
    cmd += ['--expect-prefix', str(install_dir)]
    cmd += ['--forbid-prefix', str(source_root)]
    for name in db_names:
        cmd += ['--db', name]
    env = {
        'PYTHONPATH': str(install_dir),
        'PATH': os.environ.get('PATH', ''),
    }
    with tempfile.TemporaryDirectory() as tmpdir:
        proc = subprocess.run(
            cmd, env=env, cwd=tmpdir, capture_output=True, text=True)
    if proc.returncode != 0:
        raise SystemExit(
            f'isolated dump of the installed package failed:\n{proc.stderr}')
    return json.loads(proc.stdout)


# Comparison

def _numdb_info(prefixes5, number):
    """Run the real stdnum.numdb algorithm on a prefixes tree."""
    from stdnum import numdb
    db = numdb.NumDB()
    db.prefixes = prefixes5
    return [(part, props) for part, props in db.info(number)]


def _replay_commands(name, witness, source_root, install_dir):
    script = (
        f'from stdnum import numdb; '
        f'print(numdb.get({name!r}).info({witness!r}))')
    return {
        'source': (
            f'cd {source_root} && {sys.executable} -c "{script}"'),
        'installed': (
            f'cd "$(mktemp -d)" && PYTHONPATH={install_dir} '
            f'{sys.executable} -P -c "{script}"'),
    }


def compare_db(name, length, source_dir, install_dir, dumped, alpha=ALPHABET):
    """Compare one database between the source tree and the dump of the
    installed package. Returns a report dict."""
    report = {'length': length}
    source_path = Path(source_dir) / (name + '.dat')
    source_text = source_path.read_text(encoding='utf-8')
    entry = dumped['dbs'][name]
    if 'error' in entry or 'prefixes' not in entry:
        report['status'] = 'missing_resource'
        report['error'] = entry.get('error') or entry.get('text_error')
        return report
    report['status'] = 'compared'
    src_tree = parse_dat(source_text)
    src_prefixes5 = to_numdb(src_tree)
    dst_prefixes5 = normalize5(entry['prefixes'])
    src_flat = flatten(compute_regions(src_tree, length, alpha), length, alpha)
    dst_flat = flatten(
        compute_regions(dst_prefixes5, length, alpha), length, alpha)
    source_root = str(Path(source_dir).resolve().parent)
    intervals = []
    for lo, hi, la, lb in diff(src_flat, dst_flat, alpha):
        witness = lo
        src_info = label_info(la, witness)
        dst_info = label_info(lb, witness)
        # verify the engine output against the real numdb algorithm
        assert _numdb_info(src_prefixes5, witness) == src_info
        assert _numdb_info(dst_prefixes5, witness) == dst_info
        intervals.append({
            'low': lo,
            'high': hi,
            'category': classify(la, lb),
            'witness': witness,
            'source_info': src_info,
            'installed_info': dst_info,
            'replay': _replay_commands(name, witness, source_root, install_dir),
        })
    # text-level line differences between the two data files
    src_records = dat_line_records(source_text)
    dst_text = entry.get('text') or ''
    dst_records = dat_line_records(dst_text)
    src_keys = [r[1:4] for r in src_records]
    dst_keys = [r[1:4] for r in dst_records]
    src_prov = provenance(src_flat)
    dst_prov = provenance(dst_flat)
    shadowed, observable = [], []
    remaining = list(dst_keys)
    for record, key in zip(src_records, src_keys):
        if key in remaining:
            remaining.remove(key)
            continue
        kind = 'removed'
        (shadowed if record[0] not in src_prov else observable).append(
            {'direction': kind, 'line': record[4], 'line_no': record[0]})
    dst_line_nos = {r[1:4]: r[0] for r in dst_records}
    dst_text_lines = {r[0]: r[4] for r in dst_records}
    for key in remaining:
        lineno = dst_line_nos[key]
        record = {'direction': 'added', 'line': dst_text_lines[lineno],
                  'line_no': lineno}
        (shadowed if lineno not in dst_prov else observable).append(record)
    report['intervals'] = intervals
    report['shadowed_text_diffs'] = shadowed
    report['observable_text_diffs'] = observable
    summary = {'intervals': len(intervals), 'shadowed_text_diffs': len(shadowed)}
    for category in ('shrunk', 'expanded', 'changed'):
        summary[category] = sum(
            1 for i in intervals if i['category'] == category)
    report['summary'] = summary
    return report


def _fmt_info(info):
    return '[' + ', '.join(f'({part!r}, {props})' for part, props in info) + ']'


def print_report(report):
    """Print a human-readable summary of a compare report."""
    print(f"source data:    {report['source_dir']}")
    print(f"installed pkg:  {report['installed_stdnum']}"
          f" (version {report['installed_version']})")
    for name, db in report['databases'].items():
        print(f"\n[{name}] fixed length {db['length']}")
        if db['status'] == 'missing_resource':
            print(f"  MISSING RESOURCE: {db['error']}")
            print('  every lookup of this database fails in the installed'
                  ' package; all affected import records fall back to'
                  ' unsplit/invalid handling')
            continue
        s = db['summary']
        print(f"  differing intervals: {s['intervals']}"
              f" (shrunk={s['shrunk']}, expanded={s['expanded']},"
              f" changed={s['changed']}),"
              f" shadowed text diffs: {s['shadowed_text_diffs']}")
        for interval in db['intervals']:
            print(f"  {interval['category'].upper()}"
                  f" [{interval['low']}..{interval['high']}]"
                  f" witness={interval['witness']}")
            print(f"    source:    {_fmt_info(interval['source_info'])}")
            print(f"    installed: {_fmt_info(interval['installed_info'])}")
            print(f"    replay (source):    {interval['replay']['source']}")
            print(f"    replay (installed): {interval['replay']['installed']}")
        for diff_line in db['shadowed_text_diffs']:
            print(f"  SHADOWED {diff_line['direction']}"
                  f" line {diff_line['line_no']}: {diff_line['line']}"
                  ' (fully shadowed by other rules, no lookup changes)')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest='command', required=True)
    p_dump = sub.add_parser('dump', help='dump installed package data as JSON')
    p_dump.add_argument('--db', action='append', default=[], dest='dbs')
    p_dump.add_argument('--expect-prefix', required=True)
    p_dump.add_argument('--forbid-prefix', required=True)
    p_cmp = sub.add_parser(
        'compare', help='compare source data with an installed package')
    p_cmp.add_argument('--source-dir', required=True,
                       help='directory holding the source .dat files')
    p_cmp.add_argument('--install-dir', required=True,
                       help='directory containing the installed stdnum package')
    p_cmp.add_argument(
        '--db', action='append', default=[], dest='dbs', required=True,
        help='database and declared fixed number length as name:length')
    p_cmp.add_argument('--json', dest='json_path',
                       help='write the full report to this file')
    args = parser.parse_args(argv)
    if args.command == 'dump':
        dump_installed(args.dbs, args.expect_prefix, args.forbid_prefix)
        return 0
    dbs = {}
    for spec in args.dbs:
        name, _, length = spec.rpartition(':')
        dbs[name] = int(length)
    source_dir = Path(args.source_dir).resolve()
    install_dir = Path(args.install_dir).resolve()
    source_root = source_dir.parent
    # the source-side witness check uses the source numdb implementation
    sys.path.insert(0, str(source_root))
    dumped = run_dump(list(dbs), install_dir, source_root)
    report = {
        'source_dir': str(source_dir),
        'install_dir': str(install_dir),
        'installed_stdnum': dumped['stdnum_file'],
        'installed_version': dumped['stdnum_version'],
        'databases': {},
    }
    for name, length in dbs.items():
        report['databases'][name] = compare_db(
            name, length, source_dir, install_dir, dumped)
    print_report(report)
    if args.json_path:
        with open(args.json_path, 'w', encoding='utf-8') as fp:
            json.dump(report, fp, indent=2, default=list)
    return 0


if __name__ == '__main__':
    sys.exit(main())
