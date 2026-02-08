"""
Tests for cursor awareness logic in the Emacs bridge.

These tests cover the UTF-8 byte <-> character position conversion functions
and cursor-related awareness handling in elva-bridge.py. The bridge is a
standalone script (not a package module), so we import it via importlib.
"""

import importlib.util
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from pycrdt import Awareness, Doc, Text


def _load_bridge_module():
    """Import elva-bridge.py as a module using importlib."""
    bridge_path = Path(__file__).parent.parent / "emacs" / "elva-bridge.py"
    spec = importlib.util.spec_from_file_location("elva_bridge", bridge_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bridge_mod = _load_bridge_module()
ElvaBridge = bridge_mod.ElvaBridge


def make_bridge(text_content=""):
    """Create a minimal ElvaBridge with given text content.

    The bridge constructor requires a URL but doesn't connect in __init__,
    so we can use a dummy URL for testing.
    """
    b = ElvaBridge("ws://localhost:9999/test-room-0001")
    if text_content:
        b.text.insert(0, text_content)
    return b


# ---------------------------------------------------------------------------
# _byte_pos_to_char_pos
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "byte_pos", "expected_char_pos"),
    (
        # ASCII: each character is 1 byte, so byte pos == char pos
        ("hello", 0, 0),
        ("hello", 3, 3),
        ("hello", 5, 5),
        # Empty text: only valid position is 0
        ("", 0, 0),
        # Position beyond text length: should clamp to text length
        ("hello", 100, 5),
        # 2-byte UTF-8 characters (e.g. accented letters: é = 2 bytes)
        ("café", 3, 3),   # byte 3 -> char 3 (just before 'é')
        ("café", 4, 4),   # byte 4 -> mid-sequence: orphaned lead byte becomes replacement char
        ("café", 5, 4),   # byte 5 -> char 4 (after 'é')
        # 4-byte UTF-8 characters (emoji: 🌴 = 4 bytes)
        ("\N{PALM TREE}", 0, 0),
        ("\N{PALM TREE}", 4, 1),
        # Mixed ASCII and emoji
        ("a\N{PALM TREE}b", 0, 0),   # before 'a'
        ("a\N{PALM TREE}b", 1, 1),   # after 'a', before emoji
        ("a\N{PALM TREE}b", 5, 2),   # after emoji, before 'b'
        ("a\N{PALM TREE}b", 6, 3),   # after 'b'
        # Multiple emoji in sequence
        ("\N{PALM TREE}\N{PALM TREE}", 0, 0),
        ("\N{PALM TREE}\N{PALM TREE}", 4, 1),
        ("\N{PALM TREE}\N{PALM TREE}", 8, 2),
    ),
)
def test_byte_pos_to_char_pos(text, byte_pos, expected_char_pos):
    """Convert UTF-8 byte position to character position."""
    b = make_bridge(text)
    assert b._byte_pos_to_char_pos(byte_pos) == expected_char_pos


# ---------------------------------------------------------------------------
# _char_pos_to_byte_pos
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "char_pos", "expected_byte_pos"),
    (
        # ASCII: byte pos == char pos
        ("hello", 0, 0),
        ("hello", 3, 3),
        ("hello", 5, 5),
        # Empty text
        ("", 0, 0),
        # Position beyond text length: should clamp
        ("hello", 100, 5),
        # 2-byte UTF-8 characters
        ("café", 3, 3),   # before 'é'
        ("café", 4, 5),   # after 'é' (which is 2 bytes)
        # 4-byte emoji
        ("\N{PALM TREE}", 0, 0),
        ("\N{PALM TREE}", 1, 4),
        # Mixed ASCII and emoji
        ("a\N{PALM TREE}b", 1, 1),   # after 'a'
        ("a\N{PALM TREE}b", 2, 5),   # after emoji
        ("a\N{PALM TREE}b", 3, 6),   # after 'b'
    ),
)
def test_char_pos_to_byte_pos(text, char_pos, expected_byte_pos):
    """Convert character position to UTF-8 byte position."""
    b = make_bridge(text)
    assert b._char_pos_to_byte_pos(char_pos) == expected_byte_pos


# ---------------------------------------------------------------------------
# Round-trip: byte -> char -> byte should be identity (at valid boundaries)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "byte_pos"),
    (
        # ASCII boundaries
        ("hello", 0),
        ("hello", 3),
        ("hello", 5),
        # Emoji boundaries (only valid at 0 and 4, not mid-sequence)
        ("\N{PALM TREE}", 0),
        ("\N{PALM TREE}", 4),
        # Mixed text at character boundaries
        ("a\N{PALM TREE}b", 0),
        ("a\N{PALM TREE}b", 1),
        ("a\N{PALM TREE}b", 5),
        ("a\N{PALM TREE}b", 6),
    ),
)
def test_byte_char_roundtrip(text, byte_pos):
    """Round-trip byte->char->byte should return original at character boundaries."""
    b = make_bridge(text)
    char_pos = b._byte_pos_to_char_pos(byte_pos)
    result = b._char_pos_to_byte_pos(char_pos)
    assert result == byte_pos


# ---------------------------------------------------------------------------
# _byte_count_to_char_count
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "byte_pos", "byte_count", "expected_char_count"),
    (
        # ASCII: 1 byte per char
        ("hello", 0, 3, 3),    # "hel" = 3 chars
        ("hello", 2, 2, 2),    # "ll" = 2 chars
        # Zero-length range
        ("hello", 0, 0, 0),
        # Range covering a 2-byte character
        ("café", 3, 2, 1),    # the 'é' = 2 bytes = 1 char
        # Range covering a 4-byte emoji
        ("a\N{PALM TREE}b", 1, 4, 1),   # the emoji = 4 bytes = 1 char
        # Range covering multiple characters including emoji
        ("a\N{PALM TREE}b", 0, 5, 2),   # 'a' + emoji = 2 chars
        # Range extending beyond text: should clamp
        ("hello", 3, 100, 2),  # only "lo" = 2 chars remain
        # Empty text
        ("", 0, 0, 0),
    ),
)
def test_byte_count_to_char_count(text, byte_pos, byte_count, expected_char_count):
    """Convert a byte count at a given position to character count."""
    b = make_bridge(text)
    assert b._byte_count_to_char_count(byte_pos, byte_count) == expected_char_count


# ---------------------------------------------------------------------------
# _pre_change_text: conversions should use stored text instead of current text
# ---------------------------------------------------------------------------


def test_pre_change_text_used_for_byte_to_char():
    """When _pre_change_text is set, byte->char conversion uses it.

    This is critical for delete operations from the server: the text observer
    fires after the deletion, so the current text no longer contains the
    deleted bytes. Without _pre_change_text, the position/count conversion
    would be wrong.
    """
    b = make_bridge("hello world")
    # Simulate storing pre-change text before a server delete
    b._pre_change_text = "hello world"
    # Now delete " world" from the actual yjs text (simulating server update)
    del b.text[5:11]
    # Current text is now "hello" (5 bytes), but conversion should use
    # the pre-change text "hello world" (11 bytes)
    assert b._byte_pos_to_char_pos(8) == 8   # position 8 in "hello world"
    assert b._byte_count_to_char_count(5, 6) == 6  # " world" = 6 chars


def test_pre_change_text_not_used_when_none():
    """When _pre_change_text is None, conversions use current text."""
    b = make_bridge("hello")
    assert b._pre_change_text is None
    # Position 3 in "hello" = char 3
    assert b._byte_pos_to_char_pos(3) == 3


def test_pre_change_text_with_multibyte():
    """Pre-change text conversion works correctly with multibyte characters.

    Scenario: text was "a🌴b" and the emoji gets deleted from the server.
    The delete delta references byte positions in the original text.
    """
    original = "a\N{PALM TREE}b"
    b = make_bridge(original)
    b._pre_change_text = original
    # Delete the emoji (bytes 1-4) from yjs text
    del b.text[1:5]
    # Conversion should use pre-change text, not current "ab"
    # Byte pos 5 in "a🌴b" = char pos 2 (after emoji)
    assert b._byte_pos_to_char_pos(5) == 2
    # 4 bytes at position 1 = 1 character (the emoji)
    assert b._byte_count_to_char_count(1, 4) == 1


# ---------------------------------------------------------------------------
# _on_text_change: delta processing
# ---------------------------------------------------------------------------


def test_on_text_change_insert():
    """Insert delta sends correct character position and text to Emacs."""
    b = make_bridge("hello")
    sent = []
    b._send_to_emacs = lambda msg: sent.append(msg)

    # Simulate a text change event with an insert at position 5
    event = MagicMock()
    event.delta = [{"retain": 5}, {"insert": " world"}]
    b._on_text_change(event)

    assert len(sent) == 1
    assert sent[0] == {"op": "insert", "pos": 5, "text": " world"}


def test_on_text_change_delete():
    """Delete delta sends correct character position and count to Emacs."""
    b = make_bridge("hello world")
    b._pre_change_text = "hello world"  # Set pre-change for delete conversion
    sent = []
    b._send_to_emacs = lambda msg: sent.append(msg)

    event = MagicMock()
    event.delta = [{"retain": 5}, {"delete": 6}]
    b._on_text_change(event)

    assert len(sent) == 1
    assert sent[0] == {"op": "delete", "pos": 5, "count": 6}


def test_on_text_change_insert_with_multibyte():
    """Insert of multibyte text: byte position converted to char position.

    When text is "a🌴" and we insert after the emoji (byte pos 5),
    the char position sent to Emacs should be 2.
    """
    text = "a\N{PALM TREE}"
    b = make_bridge(text)
    sent = []
    b._send_to_emacs = lambda msg: sent.append(msg)

    event = MagicMock()
    event.delta = [{"retain": 5}, {"insert": "b"}]
    b._on_text_change(event)

    assert len(sent) == 1
    assert sent[0] == {"op": "insert", "pos": 2, "text": "b"}


def test_on_text_change_suppressed_when_applying_from_emacs():
    """No messages sent to Emacs when applying changes that came from Emacs.

    The _applying_from_emacs flag prevents echoing edits back to Emacs.
    """
    b = make_bridge("hello")
    sent = []
    b._send_to_emacs = lambda msg: sent.append(msg)
    b._applying_from_emacs = True

    event = MagicMock()
    event.delta = [{"insert": "x"}]
    b._on_text_change(event)

    assert len(sent) == 0


def test_on_text_change_multiple_ops():
    """Delta with multiple operations produces multiple messages.

    A single transaction can contain retain + delete + insert in sequence.
    """
    b = make_bridge("hello world")
    b._pre_change_text = "hello world"
    sent = []
    b._send_to_emacs = lambda msg: sent.append(msg)

    # Delete "world" (5 bytes at pos 6) then insert "there"
    event = MagicMock()
    event.delta = [{"retain": 6}, {"delete": 5}, {"insert": "there"}]
    b._on_text_change(event)

    assert len(sent) == 2
    assert sent[0] == {"op": "delete", "pos": 6, "count": 5}
    assert sent[1] == {"op": "insert", "pos": 6, "text": "there"}


# ---------------------------------------------------------------------------
# _send_awareness_to_emacs: filtering and conversion
# ---------------------------------------------------------------------------


def test_awareness_filters_own_client():
    """Own client ID is excluded from the awareness message to Emacs."""
    b = make_bridge("hello")
    my_id = b.awareness.client_id

    # Set another client's state manually
    other_id = my_id + 1
    b.awareness._states[other_id] = {
        "user": {"name": "other"},
        "cursor": {"anchor": 3, "head": 3},
    }

    sent = []
    b._send_to_emacs = lambda msg: sent.append(msg)
    b._send_awareness_to_emacs()

    assert len(sent) == 1
    users = sent[0]["users"]
    # Should contain only the other client, not ourselves
    ids = [u["id"] for u in users]
    assert my_id not in ids
    assert other_id in ids


def test_awareness_skips_none_state():
    """Clients with None state (disconnected) are excluded."""
    b = make_bridge("hello")
    my_id = b.awareness.client_id

    other_id = my_id + 1
    b.awareness._states[other_id] = None

    sent = []
    b._send_to_emacs = lambda msg: sent.append(msg)
    b._send_awareness_to_emacs()

    users = sent[0]["users"]
    assert len(users) == 0


def test_awareness_converts_byte_to_char_pos():
    """Cursor byte positions are converted to character positions.

    If text is "café" and cursor is at byte 5 (after 'é'), the char
    position sent to Emacs should be 4.
    """
    b = make_bridge("café")
    my_id = b.awareness.client_id

    other_id = my_id + 1
    b.awareness._states[other_id] = {
        "user": {"name": "other"},
        "cursor": {"anchor": 5, "head": 5},
    }

    sent = []
    b._send_to_emacs = lambda msg: sent.append(msg)
    b._send_awareness_to_emacs()

    users = sent[0]["users"]
    assert users[0]["cursor"] == 4  # char pos, not byte pos


def test_awareness_uses_defaults_for_missing_fields():
    """Missing user name and color get sensible defaults."""
    b = make_bridge("hello")
    my_id = b.awareness.client_id

    other_id = my_id + 1
    b.awareness._states[other_id] = {
        "cursor": {"anchor": 0, "head": 0},
    }

    sent = []
    b._send_to_emacs = lambda msg: sent.append(msg)
    b._send_awareness_to_emacs()

    user = sent[0]["users"][0]
    assert user["name"] == f"user-{other_id}"
    assert user["color"] == "#888888"


def test_awareness_no_cursor_data():
    """Client without cursor data is included but has no 'cursor' key."""
    b = make_bridge("hello")
    my_id = b.awareness.client_id

    other_id = my_id + 1
    b.awareness._states[other_id] = {
        "user": {"name": "idle-user"},
    }

    sent = []
    b._send_to_emacs = lambda msg: sent.append(msg)
    b._send_awareness_to_emacs()

    user = sent[0]["users"][0]
    assert "cursor" not in user
    assert user["name"] == "idle-user"
