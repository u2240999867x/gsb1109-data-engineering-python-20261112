"""
Command line interface::

	python -m ignore_migration compare input.json [-o artifact.json]
	python -m ignore_migration verify input.json artifact.json

``compare`` decides whether the two rule versions behave identically over
every partition contract and writes either an equivalence proof or a
difference report with shortest witnesses. ``verify`` runs the independent
checker over such an artifact.
"""
from __future__ import annotations

import argparse
import json
import sys

from .checker import (
	verify_artifact,
)
from .engine import (
	compare,
)
from .model import (
	load_inputs,
)


def _load_json(path: str):
	with open(path, 'r', encoding='utf-8') as handle:
		return json.load(handle)


def main(argv=None) -> int:
	parser = argparse.ArgumentParser(prog='ignore_migration')
	sub = parser.add_subparsers(dest='command', required=True)

	compare_parser = sub.add_parser('compare', help='compare the two rule versions')
	compare_parser.add_argument('input', help='input JSON document')
	compare_parser.add_argument('-o', '--output', help='artifact output path (default: stdout)')

	verify_parser = sub.add_parser('verify', help='verify a proof or report')
	verify_parser.add_argument('input', help='input JSON document')
	verify_parser.add_argument('artifact', help='proof or report JSON document')

	args = parser.parse_args(argv)

	if args.command == 'compare':
		inputs = load_inputs(_load_json(args.input))
		artifact = compare(inputs)
		text = json.dumps(artifact, indent=2, ensure_ascii=False, sort_keys=True)
		if args.output:
			with open(args.output, 'w', encoding='utf-8') as handle:
				handle.write(text + '\n')
		else:
			print(text)
		return 0 if artifact['verdict'] == 'equivalent' else 1

	if args.command == 'verify':
		inputs = load_inputs(_load_json(args.input))
		artifact = _load_json(args.artifact)
		issues = verify_artifact(inputs, artifact)
		if not issues:
			print('OK: the artifact is valid for the given inputs.')
			return 0
		print(f'INVALID: {len(issues)} issue(s):')
		for issue in issues:
			location = ', '.join(
				f'{key}={value!r}' for key, value in issue.items() if key != 'message'
			)
			print(f"- {issue['message']}" + (f' [{location}]' if location else ''))
		return 1

	return 2  # pragma: no cover


if __name__ == '__main__':
	sys.exit(main())
