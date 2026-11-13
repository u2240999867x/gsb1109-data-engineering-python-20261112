import bisect
import os
import string
import io


class KeywordProcessor(object):
    """KeywordProcessor

    Attributes:
        _keyword (str): Used as key to store keywords in trie dictionary.
            Defaults to '_keyword_'
        non_word_boundaries (set(str)): Characters that will determine if the word is continuing.
            Defaults to set([A-Za-z0-9_])
        keyword_trie_dict (dict): Trie dict built character by character, that is used for lookup
            Defaults to empty dictionary
        case_sensitive (boolean): if the search algorithm should be case sensitive or not.
            Defaults to False

    Examples:
        >>> # import module
        >>> from flashtext import KeywordProcessor
        >>> # Create an object of KeywordProcessor
        >>> keyword_processor = KeywordProcessor()
        >>> # add keywords
        >>> keyword_names = ['NY', 'new-york', 'SF']
        >>> clean_names = ['new york', 'new york', 'san francisco']
        >>> for keyword_name, clean_name in zip(keyword_names, clean_names):
        >>>     keyword_processor.add_keyword(keyword_name, clean_name)
        >>> keywords_found = keyword_processor.extract_keywords('I love SF and NY. new-york is the best.')
        >>> keywords_found
        >>> ['san francisco', 'new york', 'new york']

    Note:
        * loosely based on `Aho-Corasick algorithm <https://en.wikipedia.org/wiki/Aho%E2%80%93Corasick_algorithm>`_.
        * Idea came from this `Stack Overflow Question <https://stackoverflow.com/questions/44178449/regex-replace-is-taking-time-for-millions-of-documents-how-to-make-it-faster>`_.
    """

    def __init__(self, case_sensitive=False):
        """
        Args:
            case_sensitive (boolean): Keyword search should be case sensitive set or not.
                Defaults to False
        """
        self._keyword = '_keyword_'
        self._white_space_chars = set(['.', '\t', '\n', '\a', ' ', ','])
        try:
            # python 2.x
            self.non_word_boundaries = set(string.digits + string.letters + '_')
        except AttributeError:
            # python 3.x
            self.non_word_boundaries = set(string.digits + string.ascii_letters + '_')
        self.keyword_trie_dict = dict()
        self.case_sensitive = case_sensitive
        self._terms_in_trie = 0
        self._keyword_weights = dict()
        self._default_weight = 1.0

    def __len__(self):
        """Number of terms present in the keyword_trie_dict

        Returns:
            length : int
                Count of number of distinct terms in trie dictionary.

        """
        return self._terms_in_trie

    def __contains__(self, word):
        """To check if word is present in the keyword_trie_dict

        Args:
            word : string
                word that you want to check

        Returns:
            status : bool
                If word is present as it is in keyword_trie_dict then we return True, else False

        Examples:
            >>> keyword_processor.add_keyword('Big Apple')
            >>> 'Big Apple' in keyword_processor
            >>> # True

        """
        if not self.case_sensitive:
            word = word.lower()
        current_dict = self.keyword_trie_dict
        len_covered = 0
        for char in word:
            if char in current_dict:
                current_dict = current_dict[char]
                len_covered += 1
            else:
                break
        return self._keyword in current_dict and len_covered == len(word)

    def __getitem__(self, word):
        """if word is present in keyword_trie_dict return the clean name for it.

        Args:
            word : string
                word that you want to check

        Returns:
            keyword : string
                If word is present as it is in keyword_trie_dict then we return keyword mapped to it.

        Examples:
            >>> keyword_processor.add_keyword('Big Apple', 'New York')
            >>> keyword_processor['Big Apple']
            >>> # New York
        """
        if not self.case_sensitive:
            word = word.lower()
        current_dict = self.keyword_trie_dict
        len_covered = 0
        for char in word:
            if char in current_dict:
                current_dict = current_dict[char]
                len_covered += 1
            else:
                break
        if self._keyword in current_dict and len_covered == len(word):
            return current_dict[self._keyword]

    def __setitem__(self, keyword, clean_name=None):
        """To add keyword to the dictionary
        pass the keyword and the clean name it maps to.

        Args:
            keyword : string
                keyword that you want to identify

            clean_name : string
                clean term for that keyword that you would want to get back in return or replace
                if not provided, keyword will be used as the clean name also.

        Examples:
            >>> keyword_processor['Big Apple'] = 'New York'
        """
        status = False
        if not clean_name and keyword:
            clean_name = keyword

        if keyword and clean_name:
            if not self.case_sensitive:
                keyword = keyword.lower()
            current_dict = self.keyword_trie_dict
            for letter in keyword:
                current_dict = current_dict.setdefault(letter, {})
            if self._keyword not in current_dict:
                status = True
                self._terms_in_trie += 1
            current_dict[self._keyword] = clean_name
        return status

    def __delitem__(self, keyword):
        """To remove keyword from the dictionary
        pass the keyword and the clean name it maps to.

        Args:
            keyword : string
                keyword that you want to remove if it's present

        Examples:
            >>> keyword_processor.add_keyword('Big Apple')
            >>> del keyword_processor['Big Apple']
        """
        status = False
        if keyword:
            if not self.case_sensitive:
                keyword = keyword.lower()
            current_dict = self.keyword_trie_dict
            character_trie_list = []
            for letter in keyword:
                if letter in current_dict:
                    character_trie_list.append((letter, current_dict))
                    current_dict = current_dict[letter]
                else:
                    # if character is not found, break out of the loop
                    current_dict = None
                    break
            # remove the characters from trie dict if there are no other keywords with them
            if current_dict and self._keyword in current_dict:
                # we found a complete match for input keyword.
                character_trie_list.append((self._keyword, current_dict))
                character_trie_list.reverse()

                for key_to_remove, dict_pointer in character_trie_list:
                    if len(dict_pointer.keys()) == 1:
                        dict_pointer.pop(key_to_remove)
                    else:
                        # more than one key means more than 1 path.
                        # Delete not required path and keep the other
                        dict_pointer.pop(key_to_remove)
                        break
                # successfully removed keyword
                status = True
                self._terms_in_trie -= 1
                self._keyword_weights.pop(keyword, None)
        return status

    def __iter__(self):
        """Disabled iteration as get_all_keywords() is the right way to iterate
        """
        raise NotImplementedError("Please use get_all_keywords() instead")

    def set_non_word_boundaries(self, non_word_boundaries):
        """set of characters that will be considered as part of word.

        Args:
            non_word_boundaries (set(str)):
                Set of characters that will be considered as part of word.

        """
        self.non_word_boundaries = non_word_boundaries

    def add_non_word_boundary(self, character):
        """add a character that will be considered as part of word.

        Args:
            character (char):
                Character that will be considered as part of word.

        """
        self.non_word_boundaries.add(character)

    def add_keyword(self, keyword, clean_name=None, weight=None):
        """To add one or more keywords to the dictionary
        pass the keyword and the clean name it maps to.

        Args:
            keyword : string
                keyword that you want to identify

            clean_name : string
                clean term for that keyword that you would want to get back in return or replace
                if not provided, keyword will be used as the clean name also.

            weight : float
                optional non-negative priority weight for this keyword, used only by
                `extract_keywords_with_priority`. If not provided the keyword keeps its
                current weight, or the default weight (1.0) for new keywords.

        Returns:
            status : bool
                The return value. True for success, False otherwise.

        Examples:
            >>> keyword_processor.add_keyword('Big Apple', 'New York')
            >>> # This case 'Big Apple' will return 'New York'
            >>> # OR
            >>> keyword_processor.add_keyword('Big Apple')
            >>> # This case 'Big Apple' will return 'Big Apple'
            >>> # OR, with a priority weight
            >>> keyword_processor.add_keyword('Big Apple', 'New York', weight=2.5)

        Raises:
            ValueError: If `weight` is not a non-negative number.
        """
        if weight is not None:
            self._validate_weight(weight)
        status = self.__setitem__(keyword, clean_name)
        if weight is not None and keyword and (clean_name or keyword):
            if not self.case_sensitive:
                keyword = keyword.lower()
            self._keyword_weights[keyword] = float(weight)
        return status

    @staticmethod
    def _validate_weight(weight):
        """Validate that a priority weight is a non-negative number.

        Args:
            weight : float
                weight to validate

        Raises:
            ValueError: If `weight` is not a non-negative number.
        """
        if isinstance(weight, bool) or not isinstance(weight, (int, float)):
            raise ValueError("weight must be a non-negative number, got {!r}".format(weight))
        if weight < 0:
            raise ValueError("weight must be a non-negative number, got {!r}".format(weight))

    def set_keyword_weight(self, keyword, weight):
        """Set the priority weight of a keyword already present in the dictionary.
        The weight is only used by `extract_keywords_with_priority`.

        Args:
            keyword : string
                keyword whose weight should be updated

            weight : float
                non-negative priority weight

        Returns:
            status : bool
                True if the keyword is present and the weight was set, False otherwise.

        Raises:
            ValueError: If `weight` is not a non-negative number.
        """
        self._validate_weight(weight)
        if keyword not in self:
            return False
        if not self.case_sensitive:
            keyword = keyword.lower()
        self._keyword_weights[keyword] = float(weight)
        return True

    def get_keyword_weight(self, keyword):
        """Get the priority weight of a keyword.

        Args:
            keyword : string
                keyword whose weight should be returned

        Returns:
            weight : float
                The weight assigned to the keyword, or the default weight (1.0)
                if no explicit weight was set.
        """
        if not self.case_sensitive:
            keyword = keyword.lower()
        return self._keyword_weights.get(keyword, self._default_weight)

    def remove_keyword(self, keyword):
        """To remove one or more keywords from the dictionary
        pass the keyword and the clean name it maps to.

        Args:
            keyword : string
                keyword that you want to remove if it's present

        Returns:
            status : bool
                The return value. True for success, False otherwise.

        Examples:
            >>> keyword_processor.add_keyword('Big Apple')
            >>> keyword_processor.remove_keyword('Big Apple')
            >>> # Returns True
            >>> # This case 'Big Apple' will no longer be a recognized keyword
            >>> keyword_processor.remove_keyword('Big Apple')
            >>> # Returns False

        """
        return self.__delitem__(keyword)

    def get_keyword(self, word):
        """if word is present in keyword_trie_dict return the clean name for it.

        Args:
            word : string
                word that you want to check

        Returns:
            keyword : string
                If word is present as it is in keyword_trie_dict then we return keyword mapped to it.

        Examples:
            >>> keyword_processor.add_keyword('Big Apple', 'New York')
            >>> keyword_processor.get('Big Apple')
            >>> # New York
        """
        return self.__getitem__(word)

    def add_keyword_from_file(self, keyword_file, encoding="utf-8"):
        """To add keywords from a file

        Args:
            keyword_file : path to keywords file
            encoding : specify the encoding of the file

        Examples:
            keywords file format can be like:

            >>> # Option 1: keywords.txt content
            >>> # java_2e=>java
            >>> # java programing=>java
            >>> # product management=>product management
            >>> # product management techniques=>product management

            >>> # Option 2: keywords.txt content
            >>> # java
            >>> # python
            >>> # c++

            >>> keyword_processor.add_keyword_from_file('keywords.txt')

        Raises:
            IOError: If `keyword_file` path is not valid

        """
        if not os.path.isfile(keyword_file):
            raise IOError("Invalid file path {}".format(keyword_file))
        with io.open(keyword_file, encoding=encoding) as f:
            for line in f:
                if '=>' in line:
                    keyword, clean_name = line.split('=>')
                    self.add_keyword(keyword, clean_name.strip())
                else:
                    keyword = line.strip()
                    self.add_keyword(keyword)

    def add_keywords_from_dict(self, keyword_dict):
        """To add keywords from a dictionary

        Args:
            keyword_dict (dict): A dictionary with `str` key and (list `str`) as value

        Examples:
            >>> keyword_dict = {
                    "java": ["java_2e", "java programing"],
                    "product management": ["PM", "product manager"]
                }
            >>> keyword_processor.add_keywords_from_dict(keyword_dict)

        Raises:
            AttributeError: If value for a key in `keyword_dict` is not a list.

        """
        for clean_name, keywords in keyword_dict.items():
            if not isinstance(keywords, list):
                raise AttributeError("Value of key {} should be a list".format(clean_name))

            for keyword in keywords:
                self.add_keyword(keyword, clean_name)

    def remove_keywords_from_dict(self, keyword_dict):
        """To remove keywords from a dictionary

        Args:
            keyword_dict (dict): A dictionary with `str` key and (list `str`) as value

        Examples:
            >>> keyword_dict = {
                    "java": ["java_2e", "java programing"],
                    "product management": ["PM", "product manager"]
                }
            >>> keyword_processor.remove_keywords_from_dict(keyword_dict)

        Raises:
            AttributeError: If value for a key in `keyword_dict` is not a list.

        """
        for clean_name, keywords in keyword_dict.items():
            if not isinstance(keywords, list):
                raise AttributeError("Value of key {} should be a list".format(clean_name))

            for keyword in keywords:
                self.remove_keyword(keyword)

    def add_keywords_from_list(self, keyword_list):
        """To add keywords from a list

        Args:
            keyword_list (list(str)): List of keywords to add

        Examples:
            >>> keyword_processor.add_keywords_from_list(["java", "python"]})
        Raises:
            AttributeError: If `keyword_list` is not a list.

        """
        if not isinstance(keyword_list, list):
            raise AttributeError("keyword_list should be a list")

        for keyword in keyword_list:
            self.add_keyword(keyword)

    def remove_keywords_from_list(self, keyword_list):
        """To remove keywords present in list

        Args:
            keyword_list (list(str)): List of keywords to remove

        Examples:
            >>> keyword_processor.remove_keywords_from_list(["java", "python"]})
        Raises:
            AttributeError: If `keyword_list` is not a list.

        """
        if not isinstance(keyword_list, list):
                raise AttributeError("keyword_list should be a list")

        for keyword in keyword_list:
            self.remove_keyword(keyword)

    def get_all_keywords(self, term_so_far='', current_dict=None):
        """Recursively builds a dictionary of keywords present in the dictionary
        And the clean name mapped to those keywords.

        Args:
            term_so_far : string
                term built so far by adding all previous characters
            current_dict : dict
                current recursive position in dictionary

        Returns:
            terms_present : dict
                A map of key and value where each key is a term in the keyword_trie_dict.
                And value mapped to it is the clean name mapped to it.

        Examples:
            >>> keyword_processor = KeywordProcessor()
            >>> keyword_processor.add_keyword('j2ee', 'Java')
            >>> keyword_processor.add_keyword('Python', 'Python')
            >>> keyword_processor.get_all_keywords()
            >>> {'j2ee': 'Java', 'python': 'Python'}
            >>> # NOTE: for case_insensitive all keys will be lowercased.
        """
        terms_present = {}
        if not term_so_far:
            term_so_far = ''
        if current_dict is None:
            current_dict = self.keyword_trie_dict
        for key in current_dict:
            if key == '_keyword_':
                terms_present[term_so_far] = current_dict[key]
            else:
                sub_values = self.get_all_keywords(term_so_far + key, current_dict[key])
                for key in sub_values:
                    terms_present[key] = sub_values[key]
        return terms_present

    def extract_keywords(self, sentence, span_info=False, max_cost=0):
        """Searches in the string for all keywords present in corpus.
        Keywords present are added to a list `keywords_extracted` and returned.

        Args:
            sentence (str): Line of text where we will search for keywords
            span_info (bool): True if you need to span the boundaries where the extraction has been performed
            max_cost (int): maximum levensthein distance to accept when extracting keywords

        Returns:
            keywords_extracted (list(str)): List of terms/keywords found in sentence that match our corpus

        Examples:
            >>> from flashtext import KeywordProcessor
            >>> keyword_processor = KeywordProcessor()
            >>> keyword_processor.add_keyword('Big Apple', 'New York')
            >>> keyword_processor.add_keyword('Bay Area')
            >>> keywords_found = keyword_processor.extract_keywords('I love Big Apple and Bay Area.')
            >>> keywords_found
            >>> ['New York', 'Bay Area']
            >>> keywords_found = keyword_processor.extract_keywords('I love Big Aple and Baay Area.', max_cost=1)
            >>> keywords_found
            >>> ['New York', 'Bay Area']
        """
        keywords_extracted = []
        if not sentence:
            # if sentence is empty or none just return empty list
            return keywords_extracted
        if not self.case_sensitive:
            sentence = sentence.lower()
        current_dict = self.keyword_trie_dict
        sequence_start_pos = 0
        sequence_end_pos = 0
        reset_current_dict = False
        idx = 0
        sentence_len = len(sentence)
        curr_cost = max_cost
        while idx < sentence_len:
            char = sentence[idx]
            # when we reach a character that might denote word end
            if char not in self.non_word_boundaries:

                # if end is present in current_dict
                if self._keyword in current_dict or char in current_dict:
                    # update longest sequence found
                    sequence_found = None
                    longest_sequence_found = None
                    is_longer_seq_found = False
                    if self._keyword in current_dict:
                        sequence_found = current_dict[self._keyword]
                        longest_sequence_found = current_dict[self._keyword]
                        sequence_end_pos = idx

                    # re look for longest_sequence from this position
                    if char in current_dict:
                        current_dict_continued = current_dict[char]

                        idy = idx + 1
                        while idy < sentence_len:
                            inner_char = sentence[idy]
                            if inner_char not in self.non_word_boundaries and self._keyword in current_dict_continued:
                                # update longest sequence found
                                longest_sequence_found = current_dict_continued[self._keyword]
                                sequence_end_pos = idy
                                is_longer_seq_found = True
                            if inner_char in current_dict_continued:
                                current_dict_continued = current_dict_continued[inner_char]
                            elif curr_cost > 0:
                                next_word = self.get_next_word(sentence[idy:])
                                current_dict_continued, cost, _ = next(
                                    self.levensthein(next_word, max_cost=curr_cost, start_node=current_dict_continued),
                                    ({}, 0, 0),
                                ) # current_dict_continued to empty dict by default, so next iteration goes to a `break`
                                curr_cost -= cost
                                idy += len(next_word) - 1
                                if not current_dict_continued:
                                    break
                            else:
                                break
                            idy += 1
                        else:
                            # end of sentence reached.
                            if self._keyword in current_dict_continued:
                                # update longest sequence found
                                longest_sequence_found = current_dict_continued[self._keyword]
                                sequence_end_pos = idy
                                is_longer_seq_found = True
                        if is_longer_seq_found:
                            idx = sequence_end_pos
                    current_dict = self.keyword_trie_dict
                    if longest_sequence_found:
                        keywords_extracted.append((longest_sequence_found, sequence_start_pos, idx))
                        curr_cost = max_cost
                    reset_current_dict = True
                else:
                    # we reset current_dict
                    current_dict = self.keyword_trie_dict
                    reset_current_dict = True
            elif char in current_dict:
                # we can continue from this char
                current_dict = current_dict[char]
            elif curr_cost > 0:
                next_word = self.get_next_word(sentence[idx:])
                current_dict, cost, _ = next(
                    self.levensthein(next_word, max_cost=curr_cost, start_node=current_dict),
                    (self.keyword_trie_dict, 0, 0)
                )
                curr_cost -= cost
                idx += len(next_word) - 1
            else:
                # we reset current_dict
                current_dict = self.keyword_trie_dict
                reset_current_dict = True
                # skip to end of word
                idy = idx + 1
                while idy < sentence_len:
                    char = sentence[idy]
                    if char not in self.non_word_boundaries:
                        break
                    idy += 1
                idx = idy
            # if we are end of sentence and have a sequence discovered
            if idx + 1 >= sentence_len:
                if self._keyword in current_dict:
                    sequence_found = current_dict[self._keyword]
                    keywords_extracted.append((sequence_found, sequence_start_pos, sentence_len))
            idx += 1
            if reset_current_dict:
                reset_current_dict = False
                sequence_start_pos = idx
        if span_info:
            return keywords_extracted
        return [value[0] for value in keywords_extracted]

    def _iter_keyword_candidates(self, sentence):
        """Yield every exact keyword match in the sentence as a candidate.

        A match must start at a word beginning (start of sentence, right after a
        non-word character, or on a non-word character itself) and must end right
        before a non-word character or at the end of the sentence, so word
        boundaries are respected exactly like in `extract_keywords`.

        Args:
            sentence (str): Line of text where we will search for keywords

        Yields:
            (clean_name, start, end) (tuple): clean name mapped to the matched
                keyword, and the [start, end) character span of the match.
        """
        if not self.case_sensitive:
            sentence = sentence.lower()
        sentence_len = len(sentence)
        for start in range(sentence_len):
            if (start > 0
                    and sentence[start - 1] in self.non_word_boundaries
                    and sentence[start] in self.non_word_boundaries):
                # a match cannot start in the middle of a word
                continue
            current_dict = self.keyword_trie_dict
            idx = start
            while idx < sentence_len:
                char = sentence[idx]
                if char not in current_dict:
                    break
                current_dict = current_dict[char]
                idx += 1
                if self._keyword in current_dict and (
                        idx == sentence_len or sentence[idx] not in self.non_word_boundaries):
                    yield current_dict[self._keyword], start, idx

    @staticmethod
    def _priority_state_key(state):
        """Comparison key for priority selection states. Smaller is better.

        Declared stable tie-breaking rules, applied in order:
            1. higher total weight wins;
            2. higher total character coverage wins;
            3. lexicographically smaller tuple of start offsets wins
               (earlier, and then fewer/longer, hits);
            4. lexicographically smaller tuple of clean names wins.
        """
        total_weight, coverage, starts, clean_names, _ = state
        return (-total_weight, -coverage, starts, clean_names)

    def extract_keywords_with_priority(self, sentence, span_info=False, return_conflicts=False):
        """Searches in the string for keywords, resolving overlaps by priority weights.

        Unlike `extract_keywords` (which keeps its original longest-match behaviour),
        this method collects *all* exact matches as candidates, assigns each candidate
        the weight of its keyword (see `add_keyword(..., weight=...)`, default 1.0),
        and then selects a set of non-overlapping candidates that maximizes the total
        weight over the whole sentence (weighted interval scheduling).

        Ties are resolved by the following declared stable rules, in order:
            1. higher total weight wins;
            2. higher total character coverage wins;
            3. lexicographically smaller tuple of start offsets wins
               (earlier, and then fewer/longer, hits);
            4. lexicographically smaller tuple of clean names wins.

        Args:
            sentence (str): Line of text where we will search for keywords
            span_info (bool): True if you need the span boundaries of the selected hits
            return_conflicts (bool): True to also get the competing candidates that
                overlapped a selected hit but were not selected, for manual review.
                Conflicts are always returned with span info as
                (clean_name, start, end) tuples.

        Returns:
            keywords_extracted (list): List of clean names found in the sentence, or
                list of (clean_name, start, end) tuples if `span_info` is True.
                If `return_conflicts` is True, returns a tuple
                (keywords_extracted, conflicts) instead.

        Examples:
            >>> from flashtext import KeywordProcessor
            >>> keyword_processor = KeywordProcessor()
            >>> keyword_processor.add_keyword('new york', 'New York City', weight=5.0)
            >>> keyword_processor.add_keyword('york', weight=1.0)
            >>> keyword_processor.extract_keywords_with_priority('I love new york.')
            >>> ['New York City']
            >>> keyword_processor.extract_keywords_with_priority(
            ...     'I love new york.', span_info=True, return_conflicts=True)
            >>> ([('New York City', 7, 15)], [('york', 11, 15)])
        """
        empty_result = ([], []) if return_conflicts else []
        if not sentence:
            return empty_result
        compare_sentence = sentence if self.case_sensitive else sentence.lower()
        candidates = []
        for clean_name, start, end in self._iter_keyword_candidates(sentence):
            keyword = compare_sentence[start:end]
            weight = self._keyword_weights.get(keyword, self._default_weight)
            candidates.append((clean_name, start, end, weight))
        if not candidates:
            return empty_result
        # weighted interval scheduling over candidates sorted by end offset
        candidates.sort(key=lambda candidate: (candidate[2], candidate[1], candidate[0]))
        ends = [candidate[2] for candidate in candidates]
        # dp state: (total_weight, coverage, starts, clean_names, selected_indices)
        dp = [(0.0, 0, (), (), ())]
        for idx, (clean_name, start, end, weight) in enumerate(candidates):
            prev = bisect.bisect_right(ends, start, 0, idx)
            base_weight, base_coverage, base_starts, base_names, base_selection = dp[prev]
            take = (
                base_weight + weight,
                base_coverage + (end - start),
                base_starts + (start,),
                base_names + (clean_name,),
                base_selection + (idx,),
            )
            skip = dp[-1]
            if self._priority_state_key(take) < self._priority_state_key(skip):
                dp.append(take)
            else:
                dp.append(skip)
        selected_indices = set(dp[-1][4])
        selected = sorted(
            (candidates[idx] for idx in selected_indices),
            key=lambda candidate: (candidate[1], candidate[2], candidate[0]),
        )
        if span_info:
            selected_output = [(clean_name, start, end) for clean_name, start, end, _ in selected]
        else:
            selected_output = [clean_name for clean_name, _, _, _ in selected]
        if not return_conflicts:
            return selected_output
        selected_spans = [(start, end) for _, start, end, _ in selected]
        conflicts = [
            (clean_name, start, end)
            for idx, (clean_name, start, end, _) in enumerate(candidates)
            if idx not in selected_indices
            and any(start < sel_end and sel_start < end for sel_start, sel_end in selected_spans)
        ]
        conflicts.sort(key=lambda conflict: (conflict[1], conflict[2], conflict[0]))
        return selected_output, conflicts

    def replace_keywords(self, sentence, max_cost=0):
        """Searches in the string for all keywords present in corpus.
        Keywords present are replaced by the clean name and a new string is returned.

        Args:
            sentence (str): Line of text where we will replace keywords

        Returns:
            new_sentence (str): Line of text with replaced keywords

        Examples:
            >>> from flashtext import KeywordProcessor
            >>> keyword_processor = KeywordProcessor()
            >>> keyword_processor.add_keyword('Big Apple', 'New York')
            >>> keyword_processor.add_keyword('Bay Area')
            >>> new_sentence = keyword_processor.replace_keywords('I love Big Apple and bay area.')
            >>> new_sentence
            >>> 'I love New York and Bay Area.'

        """
        if not sentence:
            # if sentence is empty or none just return the same.
            return sentence
        new_sentence = []
        orig_sentence = sentence
        if not self.case_sensitive:
            sentence = sentence.lower()
        current_word = ''
        current_dict = self.keyword_trie_dict
        current_white_space = ''
        sequence_end_pos = 0
        idx = 0
        sentence_len = len(sentence)
        curr_cost = max_cost
        while idx < sentence_len:
            char = sentence[idx]
            # when we reach whitespace
            if char not in self.non_word_boundaries:
                current_word += orig_sentence[idx]
                current_white_space = char
                # if end is present in current_dict
                if self._keyword in current_dict or char in current_dict:
                    # update longest sequence found
                    sequence_found = None
                    longest_sequence_found = None
                    is_longer_seq_found = False
                    if self._keyword in current_dict:
                        sequence_found = current_dict[self._keyword]
                        longest_sequence_found = current_dict[self._keyword]
                        sequence_end_pos = idx

                    # re look for longest_sequence from this position
                    if char in current_dict:
                        current_dict_continued = current_dict[char]
                        current_word_continued = current_word
                        idy = idx + 1
                        while idy < sentence_len:
                            inner_char = sentence[idy]
                            if inner_char not in self.non_word_boundaries and self._keyword in current_dict_continued:
                                current_word_continued += orig_sentence[idy]
                                # update longest sequence found
                                current_white_space = inner_char
                                longest_sequence_found = current_dict_continued[self._keyword]
                                sequence_end_pos = idy
                                is_longer_seq_found = True
                            if inner_char in current_dict_continued:
                                current_word_continued += orig_sentence[idy]
                                current_dict_continued = current_dict_continued[inner_char]
                            elif curr_cost > 0:
                                next_word = self.get_next_word(sentence[idy:])
                                current_dict_continued, cost, _ = next(
                                    self.levensthein(next_word, max_cost=curr_cost, start_node=current_dict_continued),
                                    ({}, 0, 0)
                                )
                                idy += len(next_word) - 1
                                curr_cost -= cost
                                current_word_continued += next_word  # just in case of a no match at the end
                                if not current_dict_continued:
                                    break
                            else:
                                break
                            idy += 1
                        else:
                            # end of sentence reached.
                            if self._keyword in current_dict_continued:
                                # update longest sequence found
                                current_white_space = ''
                                longest_sequence_found = current_dict_continued[self._keyword]
                                sequence_end_pos = idy
                                is_longer_seq_found = True
                        if is_longer_seq_found:
                            idx = sequence_end_pos
                            current_word = current_word_continued
                    current_dict = self.keyword_trie_dict
                    if longest_sequence_found:
                        curr_cost = max_cost
                        new_sentence.append(longest_sequence_found + current_white_space)
                        current_word = ''
                        current_white_space = ''
                    else:
                        new_sentence.append(current_word)
                        current_word = ''
                        current_white_space = ''
                else:
                    # we reset current_dict
                    current_dict = self.keyword_trie_dict
                    new_sentence.append(current_word)
                    current_word = ''
                    current_white_space = ''
            elif char in current_dict:
                # we can continue from this char
                current_word += orig_sentence[idx]
                current_dict = current_dict[char]
            elif curr_cost > 0:
                next_orig_word = self.get_next_word(orig_sentence[idx:])
                next_word = next_orig_word if self.case_sensitive else str.lower(next_orig_word)
                current_dict, cost, _ = next(
                    self.levensthein(next_word, max_cost=curr_cost, start_node=current_dict),
                    (self.keyword_trie_dict, 0, 0)
                )
                idx += len(next_word) - 1
                curr_cost -= cost
                current_word += next_orig_word  # just in case of a no match at the end
            else:
                current_word += orig_sentence[idx]
                # we reset current_dict
                current_dict = self.keyword_trie_dict
                # skip to end of word
                idy = idx + 1
                while idy < sentence_len:
                    char = sentence[idy]
                    current_word += orig_sentence[idy]
                    if char not in self.non_word_boundaries:
                        break
                    idy += 1
                idx = idy
                new_sentence.append(current_word)
                current_word = ''
                current_white_space = ''
            # if we are end of sentence and have a sequence discovered
            if idx + 1 >= sentence_len:
                if self._keyword in current_dict:
                    sequence_found = current_dict[self._keyword]
                    new_sentence.append(sequence_found)
                else:
                    new_sentence.append(current_word)
            idx += 1
        return "".join(new_sentence)

    def get_next_word(self, sentence):
        """
        Retrieve the next word in the sequence
        Iterate in the string until finding the first char not in non_word_boundaries

        Args:
            sentence (str): Line of text where we will look for the next word

        Returns:
            next_word (str): The next word in the sentence
        Examples:
            >>> from flashtext import KeywordProcessor
            >>> keyword_processor = KeywordProcessor()
            >>> keyword_processor.add_keyword('Big Apple')
            >>> 'Big'
        """
        next_word = str()
        for char in sentence:
            if char not in self.non_word_boundaries:
                break
            next_word += char
        return next_word

    def levensthein(self, word, max_cost=2, start_node=None):
        """
        Retrieve the nodes where there is a fuzzy match,
        via levenshtein distance, and with respect to max_cost

        Args:
            word (str): word to find a fuzzy match for
            max_cost (int): maximum levenshtein distance when performing the fuzzy match
            start_node (dict): Trie node from which the search is performed

        Yields:
            node, cost, depth (tuple): A tuple containing the final node,
                                      the cost (i.e the distance), and the depth in the trie

        Examples:
            >>> from flashtext import KeywordProcessor
            >>> keyword_processor = KeywordProcessor(case_sensitive=True)
            >>> keyword_processor.add_keyword('Marie', 'Mary')
            >>> next(keyword_processor.levensthein('Maria', max_cost=1))
            >>> ({'_keyword_': 'Mary'}, 1, 5)
            ...
            >>> keyword_processor = KeywordProcessor(case_sensitive=True
            >>> keyword_processor.add_keyword('Marie Blanc', 'Mary')
            >>> next(keyword_processor.levensthein('Mari', max_cost=1))
            >>> ({' ': {'B': {'l': {'a': {'n': {'c': {'_keyword_': 'Mary'}}}}}}}, 1, 5)
        """
        start_node = start_node or self.keyword_trie_dict
        rows = range(len(word) + 1)

        for char, node in start_node.items():
            yield from self._levenshtein_rec(char, node, word, rows, max_cost, depth=1)


    def _levenshtein_rec(self, char, node, word, rows, max_cost, depth=0):
        n_columns = len(word) + 1
        new_rows = [rows[0] + 1]
        cost = 0

        for col in range(1, n_columns):
            insert_cost = new_rows[col - 1] + 1
            delete_cost = rows[col] + 1
            replace_cost = rows[col - 1] + int(word[col - 1] != char)
            cost = min((insert_cost, delete_cost, replace_cost))
            new_rows.append(cost)

        stop_crit = isinstance(node, dict) and node.keys() & (self._white_space_chars | {self._keyword})
        if new_rows[-1] <= max_cost and stop_crit:
            yield node, cost, depth

        elif isinstance(node, dict) and min(new_rows) <= max_cost:
            for new_char, new_node in node.items():
                yield from self._levenshtein_rec(new_char, new_node, word, new_rows, max_cost, depth=depth + 1)
