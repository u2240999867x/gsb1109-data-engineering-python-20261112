"""
Differential regression of layered *.gitignore* evaluation against the local
Git executable, for partition snapshot selection.

The manifest under test mixes files inside ignored directories with worktree
files whose names match ignore patterns but which are already tracked. Git
never applies ignore rules to tracked files, so the comparison separates the
"untracked objects only" scope difference from real matching errors. For each
untracked path, both sides compute the effective selection (ignored or not)
and the deciding rule (.gitignore file, line, pattern).

When the outputs differ, the mismatch is automatically shrunk -- together with
the ancestor directories, the .gitignore level, and the rule lines -- down to a
minimal witness that still mis-selects the same partition class. A controlled
level-ordering error validates this attribution. Git is invoked with an
isolated configuration so the user's global Git config cannot influence the
oracle.
"""

import os
import shutil
import subprocess
import tempfile
from typing import (
	NamedTuple,
	Optional)  # Replaced by `X | None` in 3.10.
from unittest import (
	SkipTest,
	TestCase)

from pathspec.gitignore import (
	GitIgnoreSpec)


class RuleRef(NamedTuple):
	"""
	A reference to the rule which decided a path's selection.

	- *level* (:class:`str`) is the directory of the *.gitignore* file relative
	  to the repository root, ``''`` for the root level.
	- *line* (:class:`int`) is the 1-based line number within that file.
	- *pattern* (:class:`str`) is the raw pattern line.
	"""
	level: str
	line: int
	pattern: str

	@property
	def source(self) -> str:
		"""
		The *.gitignore* path relative to the repository root, as reported by
		`git check-ignore -v`.
		"""
		return f"{self.level}/.gitignore" if self.level else ".gitignore"


class Decision(NamedTuple):
	"""
	The effective selection of a path and the rule which decided it.

	- *ignored* (:class:`bool`) is whether the path is selected as ignored.
	- *rule* (:class:`RuleRef` or :data:`None`) is the deciding rule.
	"""
	ignored: bool
	rule: Optional[RuleRef]


class LayeredGitIgnore:
	"""
	Evaluates layered *.gitignore* files with Git's precedence: patterns from a
	deeper directory override those from a shallower one, and an excluded
	ancestor directory decides its whole subtree (deeper *.gitignore* files are
	not consulted below it).
	"""

	deepest_first = True
	"""
	Whether levels are consulted deepest-first (Git's precedence). The
	controlled ordering error flips this.
	"""

	def __init__(self, levels: dict[str, list[str]]) -> None:
		"""
		*levels* (:class:`dict` of :class:`str` to :class:`list` of :class:`str`)
		maps each directory (relative to the repository root, ``''`` for the
		root) to the lines of its *.gitignore* file.
		"""
		self._levels = {level: list(lines) for level, lines in levels.items()}
		self._specs = {
			level: GitIgnoreSpec.from_lines(lines)
			for level, lines in levels.items()
		}
		# Order levels shallowest to deepest.
		self._order = sorted(levels, key=lambda lvl: (lvl.count('/'), lvl))

	def decide(self, path: str, is_dir: bool = False) -> Decision:
		"""
		Compute the effective selection of *path* (:class:`str`), a POSIX path
		relative to the repository root. *is_dir* (:class:`bool`) is whether
		the path is queried as a directory.
		"""
		parts = path.split('/')
		# Git stops descending at the first excluded ancestor directory.
		for i in range(1, len(parts)):
			include, rule = self._match_node('/'.join(parts[:i]), True)
			if include:
				return Decision(True, rule)

		include, rule = self._match_node(path, is_dir)
		return Decision(bool(include), rule)

	def _match_node(
		self, node: str, is_dir: bool,
	) -> tuple[Optional[bool], Optional[RuleRef]]:
		"""
		Match *node* against the applicable *.gitignore* levels. A level applies
		to the paths strictly below its directory.
		"""
		levels = reversed(self._order) if self.deepest_first else iter(self._order)
		for level in levels:
			if level and not node.startswith(level + '/'):
				continue

			rel = node[len(level) + 1:] if level else node
			result = self._specs[level].check_file(f"{rel}/" if is_dir else rel)
			if result.include is not None:
				lines = self._levels[level]
				return result.include, RuleRef(level, result.index + 1, lines[result.index])

		return None, None


class ReversedLayeredGitIgnore(LayeredGitIgnore):
	"""
	The controlled ordering error: consults the shallowest *.gitignore* level
	first, inverting Git's precedence between levels.
	"""

	deepest_first = False


class Mismatch(NamedTuple):
	"""
	A path for which Git and the evaluator disagree.

	- *kind* (:class:`str`) is ``'selection'`` when the effective selections
	  differ, or ``'rule'`` when the selections agree but the deciding rules
	  differ.
	"""
	kind: str
	path: str
	git: Decision
	spec: Decision


def find_git() -> str:
	"""
	Locate the ``git`` executable.

	Raises :class:`SkipTest` if it cannot be found.
	"""
	git = shutil.which('git')
	if git is None:
		raise SkipTest("git executable not found.")
	return git


def make_git_env(home: str) -> dict[str, str]:
	"""
	Build an environment for running Git isolated from the user's
	configuration. *home* (:class:`str`) replaces ``$HOME``; the system and
	global config files are redirected to the null device.
	"""
	env = os.environ.copy()
	for var in list(env):
		if var.startswith('GIT_'):
			del env[var]

	env['HOME'] = home
	env['GIT_CONFIG_NOSYSTEM'] = '1'
	env['GIT_CONFIG_GLOBAL'] = os.devnull
	env['GIT_CONFIG_SYSTEM'] = os.devnull
	return env


def run_git(
	repo: str,
	env: dict[str, str],
	*args: str,
	input_bytes: Optional[bytes] = None,
	isolated: bool = True,
) -> subprocess.CompletedProcess:
	"""
	Run ``git`` in *repo* with *env*. When *isolated* is true, the excludes
	file is additionally pinned to the null device so no configured global
	excludes file can leak in. Returns the completed process; exit codes 0 and
	1 are accepted (1 means "no path ignored" for `check-ignore`).
	"""
	cmd = [find_git()]
	if isolated:
		cmd += ['-c', f'core.excludesFile={os.devnull}']

	cmd += args
	proc = subprocess.run(
		cmd,
		cwd=repo,
		env=env,
		input=input_bytes,
		stdout=subprocess.PIPE,
		stderr=subprocess.PIPE,
	)
	assert proc.returncode in (0, 1), (cmd, proc.returncode, proc.stderr)
	return proc


def _encode_paths(paths: list[str]) -> bytes:
	return b''.join(path.encode('utf-8') + b'\0' for path in paths)


def _level_from_source(source: str) -> str:
	"""
	Map the *.gitignore* path reported by `git check-ignore -v` back to its
	level directory.
	"""
	if source == '.gitignore':
		return ''
	elif source.endswith('/.gitignore'):
		return source[:-len('/.gitignore')]
	else:
		raise AssertionError(f"Unexpected exclude source: {source!r}")


def git_decisions(
	repo: str, env: dict[str, str], paths: list[str],
) -> dict[str, Decision]:
	"""
	Ask Git for the effective selection and deciding rule of each path. Git is
	queried twice: once for the set of ignored paths, and once verbosely for
	the deciding rule of every path (including negation-decided ones).
	"""
	ignored_proc = run_git(
		repo, env, 'check-ignore', '-z', '--stdin',
		input_bytes=_encode_paths(paths),
	)
	ignored = set(ignored_proc.stdout.decode('utf-8').split('\0'))
	ignored.discard('')

	verbose_proc = run_git(
		repo, env, 'check-ignore', '-v', '-n', '-z', '--stdin',
		input_bytes=_encode_paths(paths),
	)
	fields = verbose_proc.stdout.decode('utf-8').split('\0')
	assert fields.pop() == '', fields

	decisions = {}
	for i in range(0, len(fields), 4):
		source, line, pattern, path = fields[i:i + 4]
		if source:
			rule = RuleRef(_level_from_source(source), int(line), pattern)
		else:
			rule = None

		decisions[path] = Decision(path in ignored, rule)

	return decisions


def git_tracked(repo: str, env: dict[str, str]) -> set[str]:
	"""
	Get the tracked paths of the repository.
	"""
	proc = run_git(repo, env, 'ls-files', '-z')
	tracked = set(proc.stdout.decode('utf-8').split('\0'))
	tracked.discard('')
	return tracked


def _write_file(root: str, path: str, content: str) -> None:
	"""
	Write *content* to *path* relative to *root*, creating parent directories.
	"""
	file = os.path.join(root, *path.split('/'))
	os.makedirs(os.path.dirname(file), exist_ok=True)
	with open(file, 'w', encoding='utf-8') as fp:
		fp.write(content)


def _make_node(root: str, path: str, is_dir: bool) -> None:
	"""
	Materialize *path* on disk below *root* so Git can tell directories from
	files (a directory pattern only matches a directory path which exists).
	"""
	node = os.path.join(root, *path.split('/'))
	if is_dir:
		os.makedirs(node, exist_ok=True)
	else:
		os.makedirs(os.path.dirname(node), exist_ok=True)
		with open(node, 'wb'):
			pass


class RepoFixture:
	"""
	A temporary Git repository with layered *.gitignore* files, untracked data
	files (some inside ignored directories), and force-added tracked files
	whose names match ignore patterns.
	"""

	def __init__(
		self,
		test: TestCase,
		levels: dict[str, list[str]],
		untracked: list[str],
		tracked: list[str],
	) -> None:
		self.levels = {level: list(lines) for level, lines in levels.items()}
		self.untracked = list(untracked)
		self.tracked = list(tracked)

		self.root = tempfile.mkdtemp(prefix='pathspec-diff-')
		test.addCleanup(shutil.rmtree, self.root, True)

		self.home = os.path.join(self.root, 'home')
		os.mkdir(self.home)
		self.repo = os.path.join(self.root, 'repo')
		os.mkdir(self.repo)
		self.env = make_git_env(self.home)

		run_git(self.repo, self.env, 'init', '-q')

		for level, lines in levels.items():
			source = f"{level}/.gitignore" if level else ".gitignore"
			_write_file(self.repo, source, ''.join(f"{line}\n" for line in lines))

		for path in untracked + tracked:
			_make_node(self.repo, path, False)

		gitignore_files = [
			f"{level}/.gitignore" if level else ".gitignore" for level in levels
		]
		run_git(self.repo, self.env, 'add', '-f', '--', *(tracked + gitignore_files))
		run_git(
			self.repo, self.env,
			'-c', 'user.email=diff@example.invalid', '-c', 'user.name=diff',
			'commit', '-qm', 'init',
		)


class WitnessRepo:
	"""
	A reusable temporary repository used to re-evaluate shrunk witnesses. The
	working tree is rewritten for every evaluation; nothing is ever tracked,
	so every queried path is an untracked object.
	"""

	def __init__(self, test: TestCase) -> None:
		self.root = tempfile.mkdtemp(prefix='pathspec-witness-')
		test.addCleanup(shutil.rmtree, self.root, True)

		home = os.path.join(self.root, 'home')
		os.mkdir(home)
		self.repo = os.path.join(self.root, 'repo')
		os.mkdir(self.repo)
		self.env = make_git_env(home)
		run_git(self.repo, self.env, 'init', '-q')

	def evaluate(
		self,
		levels: dict[str, list[str]],
		path: str,
		is_dir: bool,
		evaluator_cls: type[LayeredGitIgnore],
	) -> tuple[Decision, Decision]:
		"""
		Materialize the witness and return Git's and the evaluator's decisions.
		"""
		for entry in os.listdir(self.repo):
			if entry != '.git':
				entry_path = os.path.join(self.repo, entry)
				if os.path.isdir(entry_path):
					shutil.rmtree(entry_path)
				else:
					os.unlink(entry_path)

		for level, lines in levels.items():
			source = f"{level}/.gitignore" if level else ".gitignore"
			_write_file(self.repo, source, ''.join(f"{line}\n" for line in lines))

		_make_node(self.repo, path, is_dir)

		git_dec = git_decisions(self.repo, self.env, [path])[path]
		spec_dec = evaluator_cls(levels).decide(path, is_dir)
		return git_dec, spec_dec


class Witness(NamedTuple):
	"""
	A minimal reproduction of a mismatch: the shrunk *.gitignore* levels, the
	shrunk path, and both sides' decisions.
	"""
	levels: dict[str, list[str]]
	path: str
	is_dir: bool
	git: Decision
	spec: Decision


def minimize_mismatch(
	repo: WitnessRepo,
	levels: dict[str, list[str]],
	path: str,
	is_dir: bool,
	evaluator_cls: type[LayeredGitIgnore],
	target: tuple[bool, bool],
) -> Witness:
	"""
	Shrink a mismatching case while Git's and the evaluator's effective
	selections stay in the *target* class ``(git_ignored, spec_ignored)``. The
	path is first collapsed onto its ancestor directories, then *.gitignore*
	levels and individual rule lines are dropped to a fixpoint.
	"""
	levels = {level: list(lines) for level, lines in levels.items()}

	def keeps_class(
		cand_levels: dict[str, list[str]], cand_path: str, cand_is_dir: bool,
	) -> bool:
		git_dec, spec_dec = repo.evaluate(
			cand_levels, cand_path, cand_is_dir, evaluator_cls,
		)
		return (git_dec.ignored, spec_dec.ignored) == target

	# Shrink the path onto its ancestor directories.
	changed = True
	while changed:
		changed = False
		parts = path.split('/')
		for i in range(1, len(parts)):
			ancestor = '/'.join(parts[:i])
			if keeps_class(levels, ancestor, True):
				path, is_dir = ancestor, True
				changed = True
				break

	# Drop rule-file levels and rule lines to a fixpoint.
	changed = True
	while changed:
		changed = False

		for level in sorted(levels, key=lambda lvl: lvl.count('/'), reverse=True):
			cand = {lvl: lines for lvl, lines in levels.items() if lvl != level}
			if cand and keeps_class(cand, path, is_dir):
				levels = cand
				changed = True
				break

		if changed:
			continue

		for level in list(levels):
			lines = levels[level]
			i = 0
			while i < len(lines):
				cand_lines = lines[:i] + lines[i + 1:]
				cand = {**levels, level: cand_lines}
				if cand_lines and keeps_class(cand, path, is_dir):
					levels = cand
					lines = cand_lines
					changed = True
				else:
					i += 1

	git_dec, spec_dec = repo.evaluate(levels, path, is_dir, evaluator_cls)
	return Witness(levels, path, is_dir, git_dec, spec_dec)


def run_differential(
	evaluator: LayeredGitIgnore,
	fixture: RepoFixture,
) -> tuple[list[Mismatch], dict[str, Decision]]:
	"""
	Compare the evaluator against Git over the fixture's manifest. Returns the
	mismatches and the scope differences: tracked files the evaluator would
	select as ignored but which are out of scope for Git's ignore rules.
	"""
	manifest = fixture.untracked + fixture.tracked
	git_decs = git_decisions(fixture.repo, fixture.env, manifest)
	tracked = git_tracked(fixture.repo, fixture.env) & set(manifest)

	mismatches = []
	scope_differences = {}
	for path in manifest:
		git_dec = git_decs[path]
		spec_dec = evaluator.decide(path)

		if path in tracked:
			# Git never applies ignore rules to tracked files. The evaluator
			# has no index knowledge, so a disagreement here is the expected
			# "untracked objects only" scope difference, not a matching error.
			if spec_dec.ignored:
				scope_differences[path] = spec_dec
		elif git_dec.ignored != spec_dec.ignored:
			mismatches.append(Mismatch('selection', path, git_dec, spec_dec))
		elif git_dec.rule != spec_dec.rule:
			mismatches.append(Mismatch('rule', path, git_dec, spec_dec))

	return mismatches, scope_differences


#: The layered *.gitignore* files of the standard fixture.
STANDARD_LEVELS: dict[str, list[str]] = {
	'': ['*.log', 'build/', '!keep.log'],
	'sub': ['!keep.log', '*.tmp'],
	'sub/deep': ['!special.tmp'],
}

#: Untracked manifest entries, including files inside ignored directories.
STANDARD_UNTRACKED: list[str] = [
	'a.log',                  # ignored by root "*.log".
	'keep.log',               # re-included by root "!keep.log".
	'plain.txt',              # not matched by any rule.
	'sub/keep.log',           # re-included by the deeper "sub" level.
	'sub/notes.log',          # ignored by the inherited root "*.log".
	'sub/scratch.tmp',        # ignored by "sub" level "*.tmp".
	'sub/deep/other.tmp',     # ignored by the inherited "sub" level "*.tmp".
	'sub/deep/special.tmp',   # re-included by the deepest level.
	'build/output.bin',       # inside the ignored directory "build/".
	'build/nested/notes.txt', # deeper inside the ignored directory.
]

#: Tracked manifest entries with names matching ignore patterns.
STANDARD_TRACKED: list[str] = [
	'tracked.log',            # same name class as "a.log", but tracked.
	'sub/tracked.tmp',        # matches "sub" level "*.tmp", but tracked.
	'build/tracked.txt',      # tracked file inside the ignored directory.
]


class GitDifferentialTest(TestCase):
	"""
	Cross-check the layered evaluator against the local Git executable.
	"""

	@classmethod
	def setUpClass(cls) -> None:
		find_git()

	def _make_fixture(self) -> RepoFixture:
		return RepoFixture(self, STANDARD_LEVELS, STANDARD_UNTRACKED, STANDARD_TRACKED)

	def test_clean_differential(self):
		"""
		The correct evaluator must agree with Git on the effective selection
		and the deciding rule of every untracked manifest entry, while every
		tracked entry shows up as a scope difference.
		"""
		fixture = self._make_fixture()
		mismatches, scope_differences = run_differential(
			LayeredGitIgnore(fixture.levels), fixture,
		)

		self.assertEqual(mismatches, [])
		self.assertEqual(set(scope_differences), set(STANDARD_TRACKED))

		# The manifest exercises both required categories.
		git_decs = git_decisions(fixture.repo, fixture.env, fixture.untracked)
		self.assertEqual(
			git_decs['build/output.bin'],
			Decision(True, RuleRef('', 2, 'build/')),
		)
		self.assertEqual(
			git_decs['sub/keep.log'],
			Decision(False, RuleRef('sub', 1, '!keep.log')),
		)

	def test_git_config_isolation(self):
		"""
		A hostile global Git config must not influence the oracle: the isolated
		environment ignores it, while a control run without isolation proves
		the config is potent.
		"""
		fixture = self._make_fixture()

		# Plant a global config which ignores everything.
		global_ignore = os.path.join(fixture.home, 'global_ignore')
		_write_file(fixture.home, '.gitconfig', (
			f"[core]\n\texcludesFile = {global_ignore}\n"
		))
		_write_file(fixture.home, 'global_ignore', "*\n")
		_write_file(fixture.home, '.config/git/config', (
			f"[core]\n\texcludesFile = {global_ignore}\n"
		))

		# Isolated: "plain.txt" matches no repository rule and stays selected.
		isolated_decs = git_decisions(fixture.repo, fixture.env, ['plain.txt'])
		self.assertEqual(isolated_decs['plain.txt'], Decision(False, None))

		# Control: without isolation the hostile config ignores it.
		control_env = {
			name: value for name, value in os.environ.items()
			if not name.startswith('GIT_')
		}
		control_env['HOME'] = fixture.home
		proc = subprocess.run(
			[find_git(), 'check-ignore', '-v', '-n', '-z', '--stdin'],
			cwd=fixture.repo,
			env=control_env,
			input=_encode_paths(['plain.txt']),
			stdout=subprocess.PIPE,
			stderr=subprocess.PIPE,
		)
		self.assertEqual(proc.returncode, 0)
		fields = proc.stdout.decode('utf-8').split('\0')
		self.assertEqual(fields[3], 'plain.txt')
		self.assertTrue(fields[0].endswith('global_ignore'), fields)
		self.assertEqual(fields[2], '*')

	def test_ordering_error_attribution(self):
		"""
		Inject the controlled level-ordering error and verify the differential
		flags it, and that minimization attributes it to the precedence between
		the two *.gitignore* levels.
		"""
		fixture = self._make_fixture()
		mismatches, _ = run_differential(
			ReversedLayeredGitIgnore(fixture.levels), fixture,
		)

		by_path = {mismatch.path: mismatch for mismatch in mismatches}

		# The root level's own "!keep.log" negation happens to keep the
		# selection correct for "sub/keep.log", but the deciding rule is now
		# attributed to the wrong level.
		self.assertEqual(by_path['sub/keep.log'].kind, 'rule')
		self.assertEqual(
			by_path['sub/keep.log'].spec.rule, RuleRef('', 3, '!keep.log'),
		)

		# For "sub/deep/special.tmp" the inverted precedence flips the
		# selection itself: Git re-includes the file at the deepest level,
		# while the reversed evaluator lets the shallower "*.tmp" win.
		self.assertIn('sub/deep/special.tmp', by_path)
		mismatch = by_path['sub/deep/special.tmp']
		self.assertEqual(mismatch.kind, 'selection')

		target = (mismatch.git.ignored, mismatch.spec.ignored)
		self.assertEqual(target, (False, True))

		witness = minimize_mismatch(
			WitnessRepo(self),
			fixture.levels,
			mismatch.path,
			False,
			ReversedLayeredGitIgnore,
			target,
		)

		# The witness still mis-selects the same partition class, and it cannot
		# drop either level or either rule: the fault is attributed to the
		# ordering between the two levels.
		self.assertEqual((witness.git.ignored, witness.spec.ignored), target)
		self.assertEqual(witness.path, 'sub/deep/special.tmp')
		self.assertFalse(witness.is_dir)
		self.assertEqual(witness.levels, {
			'sub': ['*.tmp'],
			'sub/deep': ['!special.tmp'],
		})
		self.assertEqual(witness.git.rule, RuleRef('sub/deep', 1, '!special.tmp'))
		self.assertEqual(witness.spec.rule, RuleRef('sub', 1, '*.tmp'))

		# The same witness evaluated with the correct precedence agrees with
		# Git, confirming the ordering was the culprit.
		git_dec, spec_dec = WitnessRepo(self).evaluate(
			witness.levels, witness.path, witness.is_dir, LayeredGitIgnore,
		)
		self.assertEqual(git_dec.ignored, spec_dec.ignored)
		self.assertEqual(git_dec.rule, spec_dec.rule)
