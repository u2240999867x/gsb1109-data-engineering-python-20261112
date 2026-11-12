"""Unrestricted Damerau-Levenshtein fuzzy matching on the FlashText trie.

The classic :meth:`KeywordProcessor.levensthein` helper only implements the
three Levenshtein operations (insertion, deletion, substitution), so an
adjacent letter transposition such as ``nike`` -> ``nkie`` is billed as two
edits.  This module adds the *unrestricted* Damerau-Levenshtein distance
(Lowrance-Wagner recurrence), where a transposition is a single edit and the
same character may take part in several consecutive edits.  For example the
distance from ``CA`` to ``ABC`` is 2 (transpose ``CA`` into ``AC``, then
insert ``B``), while plain Levenshtein and the restricted OSA variant return
3.

The search is driven by the keyword trie rather than by computing the full
string distance against every dictionary entry.  Each trie node carries one
dynamic-programming row over the text window and branches are pruned as soon
as every cell of their row exceeds the remaining budget.  Every trie node
that is actually entered is counted, so callers can prove that unrelated
prefixes are never explored to their leaves.
"""

from collections import namedtuple


# A terminal keyword reached while walking the trie.
#
# entity:     canonical (clean) name stored under the trie keyword marker
# alias:      the concrete trie keyword that matched
# end_offset: number of text characters consumed by the match
# distance:   unrestricted Damerau-Levenshtein distance of the match
TerminalHit = namedtuple(
    'TerminalHit', ('entity', 'alias', 'end_offset', 'distance')
)

# A canonical entity after every alias reaching it has been merged.
EntityCandidate = namedtuple(
    'EntityCandidate', ('entity', 'distance', 'aliases', 'span')
)


def damerau_levenshtein_distance(left, right):
    """Unrestricted Damerau-Levenshtein distance between two strings.

    Uses the Lowrance-Wagner recurrence, so transpositions count as one
    edit and a character may be involved in more than one edit.

    Args:
        left (str): first string
        right (str): second string

    Returns:
        distance (int): minimum number of insertions, deletions,
            substitutions and adjacent transpositions turning ``left``
            into ``right``.

    Examples:
        >>> damerau_levenshtein_distance('CA', 'ABC')
        2
        >>> damerau_levenshtein_distance('ab', 'ba')
        1
    """
    left_len = len(left)
    right_len = len(right)
    if left_len == 0:
        return right_len
    if right_len == 0:
        return left_len

    # One full matrix is kept: the transposition term may reach back to
    # arbitrary rows through the last-occurrence indices, so two rows are
    # not enough for the unrestricted recurrence.
    matrix = [[0] * (right_len + 1) for _ in range(left_len + 1)]
    for row in range(left_len + 1):
        matrix[row][0] = row
    for col in range(right_len + 1):
        matrix[0][col] = col

    last_row_for_char = {}
    for row in range(1, left_len + 1):
        left_char = left[row - 1]
        last_col_for_char = 0
        for col in range(1, right_len + 1):
            right_char = right[col - 1]
            trans_row = last_row_for_char.get(right_char, 0)
            trans_col = last_col_for_char
            if left_char == right_char:
                substitution_cost = 0
                last_col_for_char = col
            else:
                substitution_cost = 1

            distance = min(
                matrix[row - 1][col] + 1,                  # deletion
                matrix[row][col - 1] + 1,                  # insertion
                matrix[row - 1][col - 1] + substitution_cost,
            )
            if trans_row and trans_col:
                transposition = (
                    matrix[trans_row - 1][trans_col - 1]
                    + (row - trans_row - 1)
                    + 1
                    + (col - trans_col - 1)
                )
                distance = min(distance, transposition)
            matrix[row][col] = distance
        last_row_for_char[left_char] = row
    return matrix[left_len][right_len]


class DamerauTrieMatcher(object):
    """Budgeted unrestricted Damerau-Levenshtein search over a keyword trie.

    The matcher walks the trie depth-first and evaluates one Lowrance-Wagner
    row per trie node.  A child branch is only entered when the minimum cell
    of the current row stays within ``max_cost``; branches whose first
    characters cannot possibly catch up are therefore abandoned at once and
    their deeper nodes are never touched.

    Attributes:
        trie (dict): root of the FlashText keyword trie.
        keyword_key (str): key marking terminal trie nodes.
        max_cost (int): accepted Damerau-Levenshtein budget.
        access_count (int): number of trie nodes entered so far.
        visited_nodes (set): ``id()`` of every trie node entered so far.
    """

    def __init__(self, trie, keyword_key, max_cost, non_word_boundaries=None):
        self.trie = trie
        self.keyword_key = keyword_key
        self.max_cost = max_cost
        if non_word_boundaries is None:
            import string
            non_word_boundaries = set(
                string.digits + string.ascii_letters + '_'
            )
        self.non_word_boundaries = non_word_boundaries
        self.access_count = 0
        self.visited_nodes = set()

    def reset_counters(self):
        """Reset the node access counters before a new search."""
        self.access_count = 0
        self.visited_nodes = set()

    def search(self, sentence, start, max_end):
        """Find every terminal keyword matching ``sentence[start:max_end]``.

        Args:
            sentence (str): the whole sentence (already lower-cased when the
                processor is case-insensitive).
            start (int): offset at which the candidate span begins. It must
                sit on a word boundary.
            max_end (int): exclusive upper bound of the text window.

        Returns:
            list(EntityCandidate): one merged candidate per canonical entity
            reachable within the budget, ordered by ascending distance.
        """
        text = sentence[start:max_end]
        text_len = len(text)
        if text_len == 0:
            return []

        infinity = 10 ** 9
        base_row = list(range(text_len + 1))
        rows = [base_row]
        last_row_for_char = {}
        hits = []

        def walk(node, depth, prefix):
            self.access_count += 1
            self.visited_nodes.add(id(node))

            current_char = prefix[-1]
            previous_row = rows[-1]
            current_row = [infinity] * (text_len + 1)
            # With no text consumed, every trie character is a deletion.
            current_row[0] = depth

            last_col_for_char = 0
            for col in range(1, text_len + 1):
                text_char = text[col - 1]
                trans_row = last_row_for_char.get(text_char, 0)
                trans_col = last_col_for_char
                substitution_cost = 0 if current_char == text_char else 1
                if substitution_cost == 0:
                    last_col_for_char = col

                distance = min(
                    previous_row[col] + 1,
                    current_row[col - 1] + 1,
                    previous_row[col - 1] + substitution_cost,
                )
                if trans_row and trans_col:
                    transposition = (
                        rows[trans_row - 1][trans_col - 1]
                        + (depth - trans_row - 1)
                        + 1
                        + (col - trans_col - 1)
                    )
                    distance = min(distance, transposition)
                current_row[col] = distance

            if self.keyword_key in node:
                self._collect_terminal(
                    node, prefix, sentence, start, current_row, hits,
                )

            if min(current_row) <= self.max_cost:
                previous_occurrence = last_row_for_char.get(current_char, 0)
                last_row_for_char[current_char] = depth
                rows.append(current_row)
                for child_char, child_node in node.items():
                    if child_char == self.keyword_key:
                        continue
                    walk(child_node, depth + 1, prefix + child_char)
                rows.pop()
                if previous_occurrence:
                    last_row_for_char[current_char] = previous_occurrence
                else:
                    last_row_for_char.pop(current_char, None)

        for child_char, child_node in self.trie.items():
            if child_char == self.keyword_key:
                continue
            walk(child_node, 1, child_char)

        return self._merge_hits(hits, start)

    def _collect_terminal(self, node, prefix, sentence, start, row, hits):
        entity = node[self.keyword_key]
        sentence_len = len(sentence)
        for end_offset, distance in enumerate(row):
            if end_offset == 0 or distance > self.max_cost:
                continue
            span_end = start + end_offset
            if span_end < sentence_len and (
                sentence[span_end] in self.non_word_boundaries
            ):
                # Spans must end on a word boundary, mirroring the
                # non-fuzzy FlashText extraction semantics.
                continue
            hits.append(TerminalHit(
                entity=entity,
                alias=prefix,
                end_offset=end_offset,
                distance=distance,
            ))

    @staticmethod
    def _merge_hits(hits, start):
        # Group every terminal hit by its canonical entity and keep the
        # shortest distance only; aliases tied at that distance are merged.
        grouped = {}
        for hit in hits:
            grouped.setdefault(hit.entity, []).append(hit)

        candidates = []
        for entity, entity_hits in grouped.items():
            best_distance = min(hit.distance for hit in entity_hits)
            best_hits = [
                hit for hit in entity_hits if hit.distance == best_distance
            ]
            # Prefer the tightest span among the best-distance matches, and
            # break ties deterministically by alias name.
            best_hit = min(
                best_hits,
                key=lambda hit: (hit.end_offset, hit.alias),
            )
            # Only aliases matching the chosen span are reported, otherwise
            # an alias tied in distance on a longer span would be attached to
            # a span it does not actually cover.
            aliases = sorted({
                hit.alias for hit in best_hits
                if hit.end_offset == best_hit.end_offset
            })
            candidates.append(EntityCandidate(
                entity=entity,
                distance=best_distance,
                aliases=aliases,
                span=(start, start + best_hit.end_offset),
            ))

        candidates.sort(key=lambda candidate: (candidate.distance, candidate.entity))
        return candidates
