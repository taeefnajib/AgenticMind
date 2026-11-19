import pytest
from acme_text_nodes.nodes import word_stats
from agenticmind_node_sdk import NodeError
from agenticmind_node_sdk.testing import invoke_node_sync


def test_word_stats_counts_and_ranks():
    result = invoke_node_sync(word_stats, inputs={"text": "The cat sat. The cat ran! A dog?"}, config={"top_n": 2})
    assert result.outputs["stats"]["sentences"] == 3
    assert result.outputs["stats"]["top_words"][0] == {"word": "the", "count": 2}
    assert result.outputs["summary"].startswith("7 words")  # "A" is shorter than min_length


def test_word_stats_rejects_empty_text():
    with pytest.raises(NodeError, match="empty"):
        invoke_node_sync(word_stats, inputs={"text": "  "})
