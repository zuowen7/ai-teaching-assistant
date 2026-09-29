"""Unit tests for article_detector — multi-article PDF boundary detection."""

from src.parser.article_detector import detect_articles, extract_articles


class TestDetectByTitleAuthor:
    """Strategy A: title + author + journal info pattern."""

    def test_single_article_returns_one(self):
        text = "Just a single article with no special markers.\nMore content."
        boundaries = detect_articles(text)
        assert len(boundaries) == 1
        assert boundaries[0] == (0, len(text), "")

    def test_two_articles_detected(self):
        text = (
            "First article content here about science.\n"
            "Some paragraph with more text.\n\n"
            "Cascading Impacts of Natural Disasters\n"
            "Laurie S. Huning and Manuela I. Brunner\n"
            "Science 391 (6792), . DOI: 10.1126/science.adn8744\n"
            "Body of the second article starts here."
        )
        boundaries = detect_articles(text)
        assert len(boundaries) == 2
        # First article ends where second starts
        assert boundaries[0][1] == boundaries[1][0]
        assert boundaries[1][2] == "Cascading Impacts of Natural Disasters"

    def test_three_articles(self):
        text = (
            "Intro paragraph one.\n\n"
            "Climate Change and Global Health\n"
            "John A. Smith\n"
            "Science 400 (1234), DOI: 10.1/test\n"
            "Second article body.\n\n"
            "Machine Learning for Biology\n"
            "Jane B. Doe and Robert C. Lee\n"
            "Science 400 (1234), DOI: 10.2/test\n"
            "Third article body."
        )
        boundaries = detect_articles(text)
        assert len(boundaries) == 3


class TestDetectByTruncation:
    """Strategy B: truncated paragraph starts as boundary signals."""

    def test_truncated_n_year(self):
        text = (
            "First article about paleontology.\n"
            "More content here.\n\n"
            "n 2023, extreme heat propelled the spread.\n"
            "Second article content continues."
        )
        boundaries = detect_articles(text)
        assert len(boundaries) == 2

    def test_truncated_nflammation(self):
        text = (
            "Article about cascading impacts.\n"
            "More content here.\n\n"
            "nflammation is transient, but consequences are lifelong.\n"
            "Third article body."
        )
        boundaries = detect_articles(text)
        assert len(boundaries) == 2

    def test_noise_between_articles(self):
        text = (
            "First article text.\n"
            "Waiting to be discovered.\n"
            "ó ó\n\n"
            "n 2023, extreme heat propelled events.\n"
            "Second article text."
        )
        boundaries = detect_articles(text)
        assert len(boundaries) == 2

    def test_normal_text_not_split(self):
        text = (
            "A normal article with paragraphs.\n\n"
            "Another paragraph that starts with a capital letter.\n\n"
            "Yet another paragraph here."
        )
        boundaries = detect_articles(text)
        assert len(boundaries) == 1


class TestDetectBySplitInitial:
    """Strategy C: the extractor strands the opening word's first letter.

    Shape taken from a real Science Perspectives extraction, where the text has no
    blank line before the new article and strategy B therefore never fires.
    """

    SECOND_ARTICLE = (
        "Laurie S. Huning and Manuela I. Brunner\n"
        "I\n"
        "n 2023, extreme heat propelled the spread of severe wildfires.\n"
    )
    THIRD_ARTICLE = (
        "Guillaume Blot and Przemyslaw Sapieha\n"
        "I\n"
        "nflammation is transient, but its consequences can be lifelong.\n"
    )

    def test_stranded_initial_marks_a_boundary(self):
        text = "First article about fossils.\nSome body text.\n" + self.SECOND_ARTICLE

        boundaries = detect_articles(text)

        assert len(boundaries) == 2
        assert boundaries[1][2] == "In"

    def test_two_stranded_initials_split_three_articles(self):
        text = (
            "First article about Masripithecus.\n"
            "More about fossils.\n"
            "Multifaceted effects of extreme\n" + self.SECOND_ARTICLE + "Designed to remember\n"
            "S pecific DNA sequence features.\n" + self.THIRD_ARTICLE
        )

        articles = extract_articles(text)

        assert len(articles) == 3
        assert "Masripithecus" in articles[0]
        assert articles[1].startswith("In 2023, extreme heat")
        assert articles[2].startswith("Inflammation is transient")
        assert not articles[1].startswith("n 2023")

    def test_pronoun_i_line_is_not_a_boundary(self):
        text = (
            "A normal article.\n"
            "I\n"
            "think this sentence continues normally without truncation.\n"
            "More text follows here."
        )

        assert len(detect_articles(text)) == 1

    def test_stranded_letter_must_match_the_known_word(self):
        # "n" is a known truncation of "In", but "T" + "n 2023" is not that word.
        text = "Body text.\nT\nn 2023, something happened.\nMore text."

        assert len(detect_articles(text)) == 1


class TestExtractArticles:
    """extract_articles returns correct article texts."""

    def test_returns_original_when_single(self):
        text = "Single article content."
        articles = extract_articles(text)
        assert len(articles) == 1
        assert articles[0] == text

    def test_splits_into_parts(self):
        text = "Article one content.\n\nn 2023, something happened.\nArticle two content."
        articles = extract_articles(text)
        assert len(articles) == 2
        assert "Article one" in articles[0]
        assert "n 2023" in articles[1]

    def test_three_way_split(self):
        text = (
            "First article about Masripithecus.\n"
            "More about fossils.\n\n"
            "n 2023, extreme heat propelled events.\n"
            "Cascading impacts discussion.\n\n"
            "nflammation is transient but consequential.\n"
            "Epigenetic changes described."
        )
        articles = extract_articles(text)
        assert len(articles) == 3
        assert "Masripithecus" in articles[0]
        assert "extreme heat" in articles[1]
        assert "nflammation" in articles[2]
