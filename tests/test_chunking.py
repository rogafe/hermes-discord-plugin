from __future__ import annotations

import importlib

import pytest


@pytest.fixture
def chunking(plugin):
    return importlib.import_module(f"{plugin.__name__}.chunking")


def _check(chunking, text, limit):
    parts = chunking.split_markdown_parts(text, limit)
    chunks = ["".join(p) for p in parts]
    assert "".join(body for _p, body, _s in parts) == text
    assert all(len(c) <= limit for c in chunks), [len(c) for c in chunks]
    assert chunks == chunking.split_markdown(text, limit)
    return chunks


def test_short_text_is_single_chunk(chunking):
    assert chunking.split_markdown("hello", 10) == ["hello"]
    assert chunking.split_markdown("x" * 10, 10) == ["x" * 10]
    assert chunking.split_markdown("", 10) == [""]


def test_exact_limit_edges(chunking):
    assert _check(chunking, "a" * 20, 20) == ["a" * 20]
    chunks = _check(chunking, "a" * 21, 20)
    assert len(chunks) == 2
    chunks = _check(chunking, "aaaa bbbb\n\ncccc dddd", 11)
    assert chunks == ["aaaa bbbb\n\n", "cccc dddd"]


def test_invalid_limit(chunking):
    with pytest.raises(ValueError):
        chunking.split_markdown("abc", 0)


def test_prefers_paragraph_over_line_over_word(chunking):
    text = "para one line a\npara one line b\n\npara two is here and long enough"
    chunks = _check(chunking, text, 40)
    assert chunks[0] == "para one line a\npara one line b\n\n"
    text = "first line here\nsecond line here and more words"
    chunks = _check(chunking, text, 30)
    assert chunks[0] == "first line here\n"
    chunks = _check(chunking, "alpha beta gamma delta epsilon", 14)
    assert all(c.endswith(" ") or c == chunks[-1] for c in chunks)


def test_sentence_boundary_before_word(chunking):
    text = "One sentence ends here. Another sentence follows on and on."
    chunks = _check(chunking, text, 40)
    assert chunks[0] == "One sentence ends here. "


def test_long_single_line_without_spaces(chunking):
    text = "x" * 105
    chunks = _check(chunking, text, 20)
    assert len(chunks) == 6


def test_link_is_never_split(chunking):
    link = "[a very long link label](https://example.com/some/path?q=1)"
    text = "intro words " + link + " trailing text after the link"
    for limit in range(len(link) + 2, len(text)):
        for chunk in _check(chunking, text, limit):
            assert chunk.count("[") == chunk.count("]")
            assert ("](" in chunk) == ("[a very" in chunk)
    assert any(link in c for c in _check(chunking, text, len(link) + 5))


def test_bare_url_kept_whole(chunking):
    url = "https://example.com/" + "p" * 30
    text = "see " + url + " now"
    assert any(url in c for c in _check(chunking, text, len(url) + 4))


def test_bold_italic_code_spans_kept_whole(chunking):
    for span in ["**bold text here**", "*italic text here*", "`code span here`", "~~strike this out~~"]:
        text = "lead in words " + span + " and tail words"
        chunks = _check(chunking, text, len(span) + 6)
        assert any(span in c for c in chunks), (span, chunks)


def test_list_items_split_on_lines(chunking):
    items = [f"- item number {i} with some text" for i in range(12)]
    text = "\n".join(items)
    chunks = _check(chunking, text, 100)
    for chunk in chunks:
        assert all(line in items for line in chunk.rstrip("\n").split("\n"))


def test_code_fence_kept_whole_when_it_fits(chunking):
    block = "```python\nprint('hello')\nprint('world')\n```\n"
    text = "intro text goes here and on\n" + block + "tail"
    chunks = _check(chunking, text, len(block) + 5)
    assert any(c.startswith(block) for c in chunks)
    for chunk in chunks:
        assert chunk.count("```") % 2 == 0


def test_oversized_fence_is_closed_and_reopened(chunking):
    body = "\n".join(f"line_{i} = {i}" for i in range(40))
    text = f"before\n```python\n{body}\n```\nafter"
    parts = chunking.split_markdown_parts(text, 120)
    chunks = ["".join(p) for p in parts]
    assert "".join(b for _p, b, _s in parts) == text
    assert all(len(c) <= 120 for c in chunks)
    for chunk in chunks:
        assert chunk.count("```") % 2 == 0, chunk
    middle = [p for p in parts if p[0]]
    assert middle and all(p[0] == "```python\n" for p in middle)
    assert chunks[-1].endswith("after")


def test_tilde_fence_and_unclosed_fence(chunking):
    text = "~~~\n" + "\n".join("row %d" % i for i in range(30))
    parts = chunking.split_markdown_parts(text, 60)
    assert "".join(b for _p, b, _s in parts) == text
    assert all(len("".join(p)) <= 60 for p in parts)
    assert all(s == "~~~" or s == "\n~~~" or s == "" for _p, _b, s in parts)
    assert parts[-1][2] == ""  # the source never closed it, so neither do we


def test_fence_inner_syntax_is_not_markdown(chunking):
    text = "```\n" + "a ** b ** c _ d " * 20 + "\n```"
    _check(chunking, text, 80)


def test_unicode_and_emoji_survive(chunking):
    text = "héllo wörld 日本語のテキスト。" * 30 + "👨‍👩‍👧‍👦" * 20 + "👍🏽" * 20 + "🇫🇷" * 10
    chunks = _check(chunking, text, 17)
    joined = "".join(chunks)
    assert joined == text
    assert all(not c.startswith(("‍", "️", "\U0001f3fd")) for c in chunks)
    assert all(not c.endswith("‍") for c in chunks)


@pytest.mark.parametrize("limit", [7, 20, 64, 200, 2000])
def test_concatenation_invariant_mixed_document(chunking, limit):
    text = (
        "# Title\n\nIntro with **bold**, *it*, `code` and a [link](https://x.io/a_b).\n\n"
        "- one\n- two\n  - nested\n\n```js\nconst a = 1;\n\nconst b = 2;\n```\n\n"
        + "Sentence. " * 80 + "\n\n" + "tail " * 50
    )
    if limit < 20:
        # Tiny limits cannot hold a fence marker plus content; only the invariant matters.
        parts = chunking.split_markdown_parts(text, limit)
        assert "".join(b for _p, b, _s in parts) == text
        return
    _check(chunking, text, limit)
