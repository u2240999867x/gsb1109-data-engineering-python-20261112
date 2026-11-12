API Doc
-------

Import and initialize module
~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    >>> from flashtext import KeywordProcessor
    >>> keyword_processor = KeywordProcessor()
    >>> # if match has to be case sensitive
    >>> keyword_processor = KeywordProcessor(case_sensitive=True)

Add Keywords to module
~~~~~~~~~~~~~~~~~~~~~~
    >>> keyword_processor.add_keyword('Big Apple', 'New York')
    >>> keyword_processor.add_keyword('Bay Area')
    
Extract keywords
~~~~~~~~~~~~~~~~
    >>> keywords_found = keyword_processor.extract_keywords('I love Big Apple and Bay Area.')
    >>> keywords_found
    >>> ['New York', 'Bay Area']

Replace keywords
~~~~~~~~~~~~~~~~
    >>> keyword_processor.add_keyword('New Delhi', 'NCR region')
    >>> new_sentence = keyword_processor.replace_keywords('I love Big Apple and new delhi.')
    >>> new_sentence
    >>> 'I love New York and NCR region.'

Add keywords from File
~~~~~~~~~~~~~~~~~~~~~~
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

Add keywords from dict
~~~~~~~~~~~~~~~~~~~~~~
    >>> keyword_dict = {
            "java": ["java_2e", "java programing"],
            "product management": ["PM", "product manager"]
        }
    >>> keyword_processor.add_keywords_from_dict(keyword_dict)

Add keywords from list
~~~~~~~~~~~~~~~~~~~~~~
    >>> keyword_processor.add_keywords_from_list(["java", "python"]})

Publish a shared read-only dictionary
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    >>> from flashtext import KeywordProcessor, SharedKeywordProcessor
    >>> keyword_processor = KeywordProcessor()
    >>> keyword_processor.add_keyword('Big Apple', 'New York')
    >>> # one writer process publishes a compact mmap-friendly file
    >>> SharedKeywordProcessor.publish(keyword_processor, 'keywords.ftxd')
    >>> # any number of reader processes map the same file read-only
    >>> with SharedKeywordProcessor.open('keywords.ftxd') as shared:
    ...     shared.extract_keywords('I love Big Apple.', span_info=True)
    ...     shared.replace_keywords('I love Big Apple.')
    [('New York', 7, 16)]
    'I love New York.'
    >>> # nodes actually entered by the reader, reset with reset_counters()
    >>> shared.node_lookups
