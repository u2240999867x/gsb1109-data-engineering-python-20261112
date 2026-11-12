"""Publish a FlashText trie as a compact read-only mmap dictionary.

The regular :class:`~flashtext.keyword.KeywordProcessor` keeps its trie in
nested Python ``dict`` objects.  Every process that wants to query it has to
build (and therefore privately own) a full copy of that tree.

This module provides two pieces:

* :meth:`SharedKeywordProcessor.publish` serialises a ``KeywordProcessor``
  trie into a single compact file of fixed-width binary records.
* :meth:`SharedKeywordProcessor.open` (or simply constructing the class with
  a path) maps that file read-only with :mod:`mmap` and answers
  ``extract_keywords`` / ``replace_keywords`` queries directly against the
  mapped bytes.  No Python ``dict`` trie is ever rebuilt, so several processes
  share the same physical pages through the page cache and only touch the
  branches actually visited by a query.

Case sensitivity, keyword aliases (clean names) and the non-word-boundary
character set are all captured inside the published file, so a reader is
pinned to the dictionary version it opened.  Re-publishing to a new file never
changes the results of readers still processing an older batch.
"""

import mmap
import os
import struct


MAGIC = b'FTXD'
FORMAT_VERSION = 1

_FLAG_CASE_SENSITIVE = 1

# Header: magic, version, flags, reserved, reserved, node_count, term_count,
# charset_len, nwb_charset_offset, nwb_byte_length.
_HEADER = struct.Struct('<4sBBBBIIIII')
# Node: first_child (-1 when leaf), next_sibling (0 when last), value index
# (1-based into the string table, 0 when the node is not a keyword),
# incoming edge codepoint, number of children.
_NODE = struct.Struct('<iIIIH')
# String table entry: byte offset into the charset, byte length.
_STRING = struct.Struct('<IH')

_U16 = struct.Struct('<H')
_U32 = struct.Struct('<I')
_I32 = struct.Struct('<i')

_MAX_STRING_BYTES = 0xFFFF


class SharedKeywordProcessor(object):
    """Read-only keyword processor backed by a memory-mapped published file.

    Args:
        source: filesystem path of a published dictionary, or an already
            opened ``mmap`` object containing one.
        case_sensitive: optional sanity check.  The effective setting always
            comes from the published file; passing a conflicting value raises
            ``ValueError``.

    The object is a context manager and exposes the same query surface as
    ``KeywordProcessor``: ``extract_keywords`` (with ``span_info``),
    ``replace_keywords``, ``__contains__``, ``__getitem__``/``get_keyword``,
    ``get_all_keywords`` and ``len()``.  Fuzzy matching (``max_cost > 0``) is
    not supported on the compact read-only representation.
    """

    def __init__(self, source, case_sensitive=None):
        self._file = None
        self._owns_mapping = False
        self.path = None
        if isinstance(source, mmap.mmap):
            mapping = source
        elif isinstance(source, (str, bytes, os.PathLike)):
            path = os.fspath(source)
            if isinstance(path, bytes):
                path = path.decode('utf-8')
            self._file = open(path, 'rb')
            try:
                mapping = mmap.mmap(self._file.fileno(), length=0,
                                    access=mmap.ACCESS_READ)
            except Exception:
                self._file.close()
                self._file = None
                raise
            self._owns_mapping = True
            self.path = path
        else:
            raise TypeError(
                "source must be a file path or an mmap object, got {}".format(
                    type(source).__name__))
        self._mm = mapping

        if len(mapping) < _HEADER.size:
            self.close()
            raise ValueError(
                "file is too small to be a FlashText shared dictionary")
        header = _HEADER.unpack_from(mapping, 0)
        (magic, self.version, flags, _res0, _res1, self._node_count,
         self._term_count, charset_len, nwb_offset, nwb_len) = header
        if magic != MAGIC:
            self.close()
            raise ValueError("not a FlashText shared dictionary file")
        if self.version != FORMAT_VERSION:
            self.close()
            raise ValueError(
                "unsupported shared dictionary version {} (supported: {})"
                .format(self.version, FORMAT_VERSION))

        self.case_sensitive = bool(flags & _FLAG_CASE_SENSITIVE)
        if case_sensitive is not None and \
                bool(case_sensitive) != self.case_sensitive:
            self.close()
            raise ValueError(
                "case_sensitive={} conflicts with the published dictionary "
                "setting case_sensitive={}".format(
                    case_sensitive, self.case_sensitive))

        self._nodes_base = _HEADER.size
        self._nodes_end = self._nodes_base + self._node_count * _NODE.size
        self._charset_base = self._nodes_end
        self._charset_end = self._charset_base + charset_len
        self._strings_base = self._charset_end

        nwb_bytes = mapping[self._charset_base + nwb_offset:
                            self._charset_base + nwb_offset + nwb_len]
        self.non_word_boundaries = frozenset(nwb_bytes.decode('utf-8'))

        # Instrumentation: number of compact trie nodes examined while
        # answering queries.  Reset with ``reset_counters``.
        self._node_lookups = 0

    # ------------------------------------------------------------------
    # Publishing
    # ------------------------------------------------------------------
    @classmethod
    def publish(cls, keyword_processor, path):
        """Serialise a ``KeywordProcessor`` into a compact shared file.

        The trie is frozen at publish time: later mutations of
        ``keyword_processor`` do not affect the file.  The file is written to
        a temporary sibling and atomically renamed, so a path never exposes a
        half-written dictionary.  Publish each new dictionary version to its
        own path to keep long-lived readers on the old version.

        Args:
            keyword_processor (KeywordProcessor): the trie to publish.
            path (str): destination file path.

        Returns:
            str: the published path.
        """
        kp = keyword_processor
        trie = kp.keyword_trie_dict
        keyword_mark = kp._keyword

        # First pass: assign a stable index to every trie node (root is 0).
        order = [trie]
        index_of = {id(trie): 0}
        edge_code = {id(trie): 0}
        stack = [trie]
        while stack:
            node = stack.pop()
            children = [(ch, child) for ch, child in node.items()
                        if ch != keyword_mark]
            children.sort(key=lambda item: ord(item[0]))
            for ch, child in children:
                child_id = id(child)
                if child_id not in index_of:
                    index_of[child_id] = len(order)
                    order.append(child)
                    edge_code[child_id] = ord(ch)
                    stack.append(child)

        # Charset: non-word-boundary characters first, then de-duplicated
        # keyword clean names (aliases).
        nwb_bytes = ''.join(sorted(kp.non_word_boundaries)).encode('utf-8')
        charset_chunks = [nwb_bytes]
        cursor = len(nwb_bytes)

        value_index = {}
        node_value_index = {}
        string_entries = []
        for node in order:
            if keyword_mark not in node:
                continue
            value = node[keyword_mark]
            if not isinstance(value, str):
                raise TypeError(
                    "only string clean names can be published; "
                    "got {} for a keyword".format(type(value).__name__))
            value_bytes = value.encode('utf-8')
            if len(value_bytes) > _MAX_STRING_BYTES:
                raise ValueError(
                    "clean name longer than {} bytes cannot be published"
                    .format(_MAX_STRING_BYTES))
            if value_bytes not in value_index:
                value_index[value_bytes] = len(string_entries) + 1
                string_entries.append((cursor, len(value_bytes)))
                charset_chunks.append(value_bytes)
                cursor += len(value_bytes)
            node_value_index[id(node)] = value_index[value_bytes]
        charset = b''.join(charset_chunks)

        # Second pass: build fixed-width node records.
        records = [None] * len(order)
        sibling_links = []
        for node in order:
            node_id = id(node)
            children = sorted(
                (ch for ch in node.keys() if ch != keyword_mark),
                key=ord)
            child_indices = [index_of[id(node[ch])] for ch in children]
            value = node_value_index.get(node_id, 0)
            first_child = child_indices[0] if child_indices else -1
            if len(children) > 0xFFFF:
                raise ValueError(
                    "a trie node has {} children; the compact format supports "
                    "at most 65535 edges per node".format(len(children)))
            records[index_of[node_id]] = [
                first_child, 0, value, edge_code[node_id], len(children)]
            sibling_links.extend(zip(child_indices, child_indices[1:]))
        for sibling, next_sibling in sibling_links:
            records[sibling][1] = next_sibling

        term_count = sum(1 for node in order if keyword_mark in node)
        flags = _FLAG_CASE_SENSITIVE if kp.case_sensitive else 0

        path = os.fspath(path)
        if isinstance(path, bytes):
            path = path.decode('utf-8')
        tmp_path = path + '.tmp'
        with open(tmp_path, 'wb') as f:
            f.write(_HEADER.pack(
                MAGIC, FORMAT_VERSION, flags, 0, 0,
                len(order), term_count, len(charset), 0, len(nwb_bytes)))
            for record in records:
                f.write(_NODE.pack(*record))
            f.write(charset)
            for offset, length in string_entries:
                f.write(_STRING.pack(offset, length))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
        return path

    @classmethod
    def open(cls, path, case_sensitive=None):
        """Open a published shared dictionary from ``path``."""
        return cls(path, case_sensitive=case_sensitive)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def close(self):
        """Close the memory mapping (and the underlying file)."""
        if self._owns_mapping and getattr(self, '_mm', None) is not None:
            try:
                self._mm.close()
            except (BufferError, ValueError):
                pass
        self._mm = None
        if self._file is not None:
            self._file.close()
            self._file = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
        return False

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    @property
    def closed(self):
        return self._mm is None

    def __len__(self):
        return self._term_count

    def reset_counters(self):
        """Reset the node-visit instrumentation counter to zero."""
        self._node_lookups = 0

    @property
    def node_lookups(self):
        """Number of compact trie nodes examined since the last reset."""
        return self._node_lookups

    # ------------------------------------------------------------------
    # Low-level navigation over the mapped bytes
    # ------------------------------------------------------------------
    def _node_base(self, node_idx):
        return self._nodes_base + node_idx * _NODE.size

    def _has_value(self, node_idx):
        value_idx_pos = self._node_base(node_idx) + 8
        return _U32.unpack_from(self._mm, value_idx_pos)[0] != 0

    def _value(self, node_idx):
        value_idx = _U32.unpack_from(
            self._mm, self._node_base(node_idx) + 8)[0]
        entry_pos = self._strings_base + (value_idx - 1) * _STRING.size
        offset, length = _STRING.unpack_from(self._mm, entry_pos)
        start = self._charset_base + offset
        return self._mm[start:start + length].decode('utf-8')

    def _child(self, node_idx, char):
        """Return the index of the child reached by ``char`` or -1."""
        base = self._node_base(node_idx)
        edge_len = _U16.unpack_from(self._mm, base + 16)[0]
        if edge_len == 0:
            return -1
        current = _I32.unpack_from(self._mm, base)[0]
        target_code = ord(char)
        for _ in range(edge_len):
            child_base = self._node_base(current)
            edge_code = _U32.unpack_from(self._mm, child_base + 12)[0]
            if edge_code == target_code:
                # Only the branch actually followed counts as a visited
                # node: sibling probes stay inside the shared mapping and
                # never materialise a node in the reader process.
                self._node_lookups += 1
                return current
            next_sibling = _U32.unpack_from(self._mm, child_base + 4)[0]
            if next_sibling == 0:
                return -1
            current = next_sibling
        return -1

    def _walk(self, word):
        current = 0
        for char in word:
            child = self._child(current, char)
            if child < 0:
                return current, False
            current = child
        return current, True

    # ------------------------------------------------------------------
    # KeywordProcessor-compatible query surface
    # ------------------------------------------------------------------
    def __contains__(self, word):
        if not self.case_sensitive:
            word = word.lower()
        node_idx, fully_consumed = self._walk(word)
        return fully_consumed and self._has_value(node_idx)

    def __getitem__(self, word):
        if not self.case_sensitive:
            word = word.lower()
        node_idx, fully_consumed = self._walk(word)
        if fully_consumed and self._has_value(node_idx):
            return self._value(node_idx)
        # Mirrors KeywordProcessor, which returns None for unknown terms.
        return None

    def get_keyword(self, word):
        """Return the clean name for ``word`` or ``None`` when absent."""
        if not self.case_sensitive:
            word = word.lower()
        node_idx, fully_consumed = self._walk(word)
        if fully_consumed and self._has_value(node_idx):
            return self._value(node_idx)
        return None

    def get_all_keywords(self):
        """Return ``{keyword: clean_name}`` for every published term."""
        terms = {}
        stack = [(0, '')]
        while stack:
            node_idx, prefix = stack.pop()
            base = self._node_base(node_idx)
            if self._has_value(node_idx):
                terms[prefix] = self._value(node_idx)
            edge_len = _U16.unpack_from(self._mm, base + 16)[0]
            if edge_len:
                current = _I32.unpack_from(self._mm, base)[0]
                collected = []
                for _ in range(edge_len):
                    child_base = self._node_base(current)
                    code = _U32.unpack_from(self._mm, child_base + 12)[0]
                    collected.append((current, chr(code)))
                    next_sibling = _U32.unpack_from(
                        self._mm, child_base + 4)[0]
                    if next_sibling == 0:
                        break
                    current = next_sibling
                for child_idx, char in reversed(collected):
                    stack.append((child_idx, prefix + char))
        return terms

    def extract_keywords(self, sentence, span_info=False, max_cost=0):
        """Extract keywords from ``sentence`` using the mapped dictionary.

        Results (including span boundaries) are identical to
        ``KeywordProcessor.extract_keywords`` for ``max_cost=0``.
        """
        if max_cost:
            raise ValueError(
                "fuzzy matching (max_cost > 0) is not supported by "
                "SharedKeywordProcessor")
        keywords_extracted = []
        if not sentence:
            return keywords_extracted
        if not self.case_sensitive:
            sentence = sentence.lower()
        non_word_boundaries = self.non_word_boundaries
        current_dict = 0
        sequence_start_pos = 0
        sequence_end_pos = 0
        reset_current_dict = False
        idx = 0
        sentence_len = len(sentence)
        while idx < sentence_len:
            char = sentence[idx]
            if char not in non_word_boundaries:
                child = self._child(current_dict, char)
                if self._has_value(current_dict) or child >= 0:
                    longest_sequence_found = None
                    is_longer_seq_found = False
                    if self._has_value(current_dict):
                        longest_sequence_found = self._value(current_dict)
                        sequence_end_pos = idx

                    if child >= 0:
                        current_dict_continued = child
                        idy = idx + 1
                        while idy < sentence_len:
                            inner_char = sentence[idy]
                            if inner_char not in non_word_boundaries and \
                                    self._has_value(current_dict_continued):
                                longest_sequence_found = \
                                    self._value(current_dict_continued)
                                sequence_end_pos = idy
                                is_longer_seq_found = True
                            next_node = self._child(
                                current_dict_continued, inner_char)
                            if next_node >= 0:
                                current_dict_continued = next_node
                            else:
                                break
                            idy += 1
                        else:
                            if self._has_value(current_dict_continued):
                                longest_sequence_found = \
                                    self._value(current_dict_continued)
                                sequence_end_pos = idy
                                is_longer_seq_found = True
                        if is_longer_seq_found:
                            idx = sequence_end_pos
                    current_dict = 0
                    if longest_sequence_found:
                        keywords_extracted.append(
                            (longest_sequence_found, sequence_start_pos, idx))
                    reset_current_dict = True
                else:
                    current_dict = 0
                    reset_current_dict = True
            else:
                next_node = self._child(current_dict, char)
                if next_node >= 0:
                    current_dict = next_node
                else:
                    current_dict = 0
                    reset_current_dict = True
                    idy = idx + 1
                    while idy < sentence_len:
                        if sentence[idy] not in non_word_boundaries:
                            break
                        idy += 1
                    idx = idy
            if idx + 1 >= sentence_len:
                if self._has_value(current_dict):
                    sequence_found = self._value(current_dict)
                    keywords_extracted.append(
                        (sequence_found, sequence_start_pos, sentence_len))
            idx += 1
            if reset_current_dict:
                reset_current_dict = False
                sequence_start_pos = idx
        if span_info:
            return keywords_extracted
        return [value[0] for value in keywords_extracted]

    def replace_keywords(self, sentence, max_cost=0):
        """Replace keywords in ``sentence`` using the mapped dictionary."""
        if max_cost:
            raise ValueError(
                "fuzzy matching (max_cost > 0) is not supported by "
                "SharedKeywordProcessor")
        if not sentence:
            return sentence
        new_sentence = []
        orig_sentence = sentence
        if not self.case_sensitive:
            sentence = sentence.lower()
        non_word_boundaries = self.non_word_boundaries
        current_word = ''
        current_dict = 0
        current_white_space = ''
        sequence_end_pos = 0
        idx = 0
        sentence_len = len(sentence)
        while idx < sentence_len:
            char = sentence[idx]
            if char not in non_word_boundaries:
                current_word += orig_sentence[idx]
                current_white_space = char
                child = self._child(current_dict, char)
                if self._has_value(current_dict) or child >= 0:
                    longest_sequence_found = None
                    is_longer_seq_found = False
                    if self._has_value(current_dict):
                        longest_sequence_found = self._value(current_dict)
                        sequence_end_pos = idx

                    if child >= 0:
                        current_dict_continued = child
                        current_word_continued = current_word
                        idy = idx + 1
                        while idy < sentence_len:
                            inner_char = sentence[idy]
                            if inner_char not in non_word_boundaries and \
                                    self._has_value(current_dict_continued):
                                current_word_continued += orig_sentence[idy]
                                current_white_space = inner_char
                                longest_sequence_found = \
                                    self._value(current_dict_continued)
                                sequence_end_pos = idy
                                is_longer_seq_found = True
                            next_node = self._child(
                                current_dict_continued, inner_char)
                            if next_node >= 0:
                                current_word_continued += orig_sentence[idy]
                                current_dict_continued = next_node
                            else:
                                break
                            idy += 1
                        else:
                            if self._has_value(current_dict_continued):
                                current_white_space = ''
                                longest_sequence_found = \
                                    self._value(current_dict_continued)
                                sequence_end_pos = idy
                                is_longer_seq_found = True
                        if is_longer_seq_found:
                            idx = sequence_end_pos
                            current_word = current_word_continued
                    current_dict = 0
                    if longest_sequence_found:
                        new_sentence.append(
                            longest_sequence_found + current_white_space)
                        current_word = ''
                        current_white_space = ''
                    else:
                        new_sentence.append(current_word)
                        current_word = ''
                        current_white_space = ''
                else:
                    current_dict = 0
                    new_sentence.append(current_word)
                    current_word = ''
                    current_white_space = ''
            else:
                next_node = self._child(current_dict, char)
                if next_node >= 0:
                    current_word += orig_sentence[idx]
                    current_dict = next_node
                else:
                    current_word += orig_sentence[idx]
                    current_dict = 0
                    idy = idx + 1
                    while idy < sentence_len:
                        inner_char = sentence[idy]
                        current_word += orig_sentence[idy]
                        if inner_char not in non_word_boundaries:
                            break
                        idy += 1
                    idx = idy
                    new_sentence.append(current_word)
                    current_word = ''
                    current_white_space = ''
            if idx + 1 >= sentence_len:
                if self._has_value(current_dict):
                    sequence_found = self._value(current_dict)
                    new_sentence.append(sequence_found)
                else:
                    new_sentence.append(current_word)
            idx += 1
        return ''.join(new_sentence)
