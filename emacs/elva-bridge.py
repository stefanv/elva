#!/usr/bin/env python3
"""
Bridge between Emacs and Elva using pycrdt.

This bridge subprocess handles the Yjs protocol communication with an Elva server,
translating between Yjs CRDT operations and simple position-based edits for Emacs.
"""

import asyncio
import json
import sys
from pycrdt import Awareness, Doc, Text
import websockets


# Y-Protocol message types (magic bytes)
SYNC = 0
SYNC_STEP1 = 0  # (0, 0) - send state vector
SYNC_STEP2 = 1  # (0, 1) - reply with update
SYNC_UPDATE = 2  # (0, 2) - incremental update
AWARENESS = 1  # (1,) - awareness/presence


def encode_message(msg_type: tuple[int, ...], payload: bytes) -> bytes:
    """Encode a Y-protocol message with magic bytes and length-prefixed payload."""
    # Magic bytes
    magic = bytes(msg_type)
    # Variable-length encode the payload length
    length = len(payload)
    len_bytes = _encode_var_uint(length)
    return magic + len_bytes + payload


def _encode_var_uint(num: int) -> bytes:
    """Encode an integer as a variable-length unsigned int."""
    result = []
    while num > 127:
        result.append(128 | (num & 127))
        num >>= 7
    result.append(num)
    return bytes(result)


def _decode_var_uint(data: bytes, offset: int = 0) -> tuple[int, int]:
    """Decode a variable-length unsigned int. Returns (value, bytes_consumed)."""
    result = 0
    shift = 0
    idx = offset
    while True:
        byte = data[idx]
        result |= (byte & 127) << shift
        idx += 1
        if byte < 128:
            break
        shift += 7
    return result, idx - offset


def decode_message(data: bytes) -> tuple[tuple[int, ...], bytes]:
    """Decode a Y-protocol message. Returns (msg_type, payload)."""
    offset = 0

    # Read first magic byte
    mb1, consumed = _decode_var_uint(data, offset)
    offset += consumed

    if mb1 == AWARENESS:
        # Awareness has single magic byte
        msg_type = (AWARENESS,)
    else:
        # Sync messages have two magic bytes
        mb2, consumed = _decode_var_uint(data, offset)
        offset += consumed
        msg_type = (mb1, mb2)

    # Read payload length
    payload_len, consumed = _decode_var_uint(data, offset)
    offset += consumed

    # Extract payload
    payload = data[offset : offset + payload_len]
    return msg_type, payload


class ElvaBridge:
    """Bridge between Emacs (via stdio) and Elva (via WebSocket)."""

    def __init__(self, url: str, user_name: str = "emacs"):
        # Append client identifier to URL
        separator = "&" if "?" in url else "?"
        self.url = f"{url}{separator}client=emacs"
        self.doc = Doc()
        # Use "ytext" to match Elva's editor client
        self.text = self.doc.get("ytext", type=Text)
        self._applying_from_server = False  # Block echo to Emacs
        self._applying_from_emacs = False   # Block echo to Emacs (but send to server)
        self._initial_sync_done = False     # Track initial sync completion
        self._pre_change_text = None        # Text before server update (for delete char count)
        self._ws = None
        self._send_queue = asyncio.Queue()

        # Awareness for cursor/presence syncing
        self.awareness = Awareness(self.doc)
        self.awareness.set_local_state({"user": {"name": user_name}})
        self._awareness_queue = asyncio.Queue()

    def _byte_pos_to_char_pos(self, byte_pos: int) -> int:
        """Convert UTF-8 byte position to character position.

        Uses pre-change text if available (for operations from server),
        otherwise uses current text.
        """
        # Use pre-change text if available (needed for position conversion during server updates)
        text_str = self._pre_change_text if self._pre_change_text else str(self.text)
        text_bytes = text_str.encode("utf-8")
        # Clamp to valid range
        byte_pos = min(byte_pos, len(text_bytes))
        # Decode the bytes up to byte_pos to get character count
        prefix = text_bytes[:byte_pos].decode("utf-8", errors="replace")
        return len(prefix)

    def _char_pos_to_byte_pos(self, char_pos: int) -> int:
        """Convert character position to UTF-8 byte position."""
        text_str = str(self.text)
        # Clamp to valid range
        char_pos = min(char_pos, len(text_str))
        # Encode the characters up to char_pos to get byte count
        prefix = text_str[:char_pos]
        return len(prefix.encode("utf-8"))

    def _byte_count_to_char_count(self, byte_pos: int, byte_count: int) -> int:
        """Convert UTF-8 byte count to character count at a given position.

        Uses pre-change text if available (for delete operations from server),
        otherwise uses current text (for local operations).
        """
        # Use pre-change text if available (needed for deletes from server)
        text_str = self._pre_change_text if self._pre_change_text else str(self.text)
        text_bytes = text_str.encode("utf-8")
        # Get the substring in bytes and decode to get character count
        end_pos = min(byte_pos + byte_count, len(text_bytes))
        substring_bytes = text_bytes[byte_pos:end_pos]
        return len(substring_bytes.decode("utf-8", errors="replace"))

    def _send_to_emacs(self, msg: dict):
        """Send JSON message to Emacs via stdout."""
        print(json.dumps(msg), flush=True)

    def _log(self, msg: str):
        """Log to stderr (visible in Emacs process buffer)."""
        print(f"[bridge] {msg}", file=sys.stderr, flush=True)

    def _send_awareness_to_emacs(self):
        """Send other users' cursor positions to Emacs."""
        my_id = self.awareness.client_id

        users = []
        for client_id, state in self.awareness._states.items():
            if client_id == my_id:
                continue
            if state is None:
                continue

            user_info = state.get("user", {})
            cursor = state.get("cursor")
            user_data = {
                "id": client_id,
                "name": user_info.get("name", f"user-{client_id}"),
                "color": user_info.get("color", "#888888"),
            }
            if cursor is not None:
                # Convert byte position to char position
                byte_pos = cursor.get("anchor", cursor.get("head", 0))
                char_pos = self._byte_pos_to_char_pos(byte_pos)
                user_data["cursor"] = char_pos
            users.append(user_data)
        self._send_to_emacs({"op": "awareness", "users": users})

    def _update_local_cursor(self, char_pos: int):
        """Update our cursor position in awareness state."""
        byte_pos = self._char_pos_to_byte_pos(char_pos)
        state = self.awareness.get_local_state() or {}
        state["cursor"] = {"anchor": byte_pos, "head": byte_pos}
        self.awareness.set_local_state(state)
        # Queue awareness update for server
        client_ids = [self.awareness.client_id]
        payload = self.awareness.encode_awareness_update(client_ids)
        self._awareness_queue.put_nowait(payload)

    def _on_text_change(self, event):
        """Called when Yjs text changes. Translate to Emacs operations."""
        # Don't echo back to Emacs if we're applying changes FROM Emacs
        # DO send to Emacs if changes are from server
        if self._applying_from_emacs:
            return

        # Delta positions are in UTF-8 bytes, Emacs needs character positions
        byte_pos = 0
        for delta in event.delta:
            if "retain" in delta:
                byte_pos += delta["retain"]
            elif "insert" in delta:
                text = delta["insert"]
                char_pos = self._byte_pos_to_char_pos(byte_pos)
                self._send_to_emacs({"op": "insert", "pos": char_pos, "text": text})
                byte_pos += len(text.encode("utf-8"))
            elif "delete" in delta:
                byte_count = delta["delete"]
                char_pos = self._byte_pos_to_char_pos(byte_pos)
                char_count = self._byte_count_to_char_count(byte_pos, byte_count)
                self._send_to_emacs({"op": "delete", "pos": char_pos, "count": char_count})

    def _apply_from_emacs(self, msg: dict):
        """Apply an edit from Emacs to the Yjs doc.

        Emacs sends character positions, but pycrdt uses UTF-8 byte positions.
        """
        self._applying_from_emacs = True  # Don't echo back to Emacs
        try:
            op = msg.get("op")
            if op == "insert":
                char_pos = msg["pos"]
                byte_pos = self._char_pos_to_byte_pos(char_pos)
                self.text.insert(byte_pos, msg["text"])
            elif op == "delete":
                char_pos = msg["pos"]
                char_count = msg["count"]
                byte_pos = self._char_pos_to_byte_pos(char_pos)
                # Calculate byte count from character count
                text_str = str(self.text)
                end_char_pos = min(char_pos + char_count, len(text_str))
                byte_end = self._char_pos_to_byte_pos(end_char_pos)
                byte_count = byte_end - byte_pos
                del self.text[byte_pos:byte_pos + byte_count]
            elif op == "sync":
                # Full sync request - clear and set content
                with self.doc.transaction():
                    if len(self.text) > 0:
                        del self.text[0:len(self.text)]
                    if msg.get("text"):
                        self.text.insert(0, msg["text"])
            elif op == "cursor":
                # Update our cursor position for awareness
                char_pos = msg.get("pos", 0)
                self._update_local_cursor(char_pos)
        finally:
            self._applying_from_emacs = False

    async def _send_sync_step1(self):
        """Send SYNC_STEP1 with our state vector."""
        state = bytes(self.doc.get_state())
        msg = encode_message((SYNC, SYNC_STEP1), state)
        await self._ws.send(msg)
        self._log("sent sync step 1")

    async def _send_sync_step2(self, their_state: bytes):
        """Send SYNC_STEP2 with update relative to their state."""
        update = bytes(self.doc.get_update(their_state))
        msg = encode_message((SYNC, SYNC_STEP2), update)
        await self._ws.send(msg)
        self._log("sent sync step 2")

    async def _send_update(self):
        """Send incremental update to server."""
        # Get update since last sync (from empty state for now)
        # In practice, we send the transaction update via observer
        pass

    async def _on_ws_message(self, data: bytes):
        """Handle incoming WebSocket message."""
        try:
            msg_type, payload = decode_message(data)
        except Exception as e:
            self._log(f"failed to decode message: {e}")
            return

        if msg_type == (SYNC, SYNC_STEP1):
            # Server is asking for our state
            await self._send_sync_step2(payload)
            self._log("received sync step 1, replied with step 2")

        elif msg_type == (SYNC, SYNC_STEP2) or msg_type == (SYNC, SYNC_UPDATE):
            # Server is sending us updates
            if payload != b"\x00\x00":  # Not empty update
                # Store pre-change text for position conversion in observer
                self._pre_change_text = str(self.text)
                self._applying_from_server = True  # Don't echo to Emacs or send back
                try:
                    self.doc.apply_update(payload)
                    self._log(f"applied update ({len(payload)} bytes)")
                finally:
                    self._applying_from_server = False
                    self._pre_change_text = None

            # After first sync, notify Emacs of initial room content
            if not self._initial_sync_done:
                self._initial_sync_done = True
                content = str(self.text)
                self._send_to_emacs({
                    "op": "sync_complete",
                    "length": len(content),
                    "content": content
                })
                self._log(f"initial sync complete, room has {len(content)} chars")

        elif msg_type == (AWARENESS,):
            # Apply awareness update and notify Emacs of other users' cursors
            self.awareness.apply_awareness_update(payload, origin="remote")
            self._send_awareness_to_emacs()
            self._log("received and applied awareness update")

        else:
            self._log(f"unknown message type: {msg_type}")

    def _on_doc_update(self, event):
        """Called when local doc changes - queue update for server."""
        # Only skip if applying from server (to avoid echo)
        # We DO want to send when applying from Emacs
        if self._applying_from_server:
            return
        if event.update and event.update != b"\x00\x00":
            self._send_queue.put_nowait(event.update)

    async def _ws_sender(self):
        """Send queued updates to WebSocket."""
        while True:
            update = await self._send_queue.get()
            if self._ws:
                msg = encode_message((SYNC, SYNC_UPDATE), bytes(update))
                await self._ws.send(msg)
                self._log(f"sent update ({len(update)} bytes)")

    async def _awareness_sender(self):
        """Send queued awareness updates to WebSocket."""
        while True:
            payload = await self._awareness_queue.get()
            if self._ws:
                msg = encode_message((AWARENESS,), bytes(payload))
                await self._ws.send(msg)
                self._log("sent awareness update")

    async def _ws_receiver(self):
        """Receive messages from WebSocket."""
        try:
            async for data in self._ws:
                await self._on_ws_message(data)
        except websockets.ConnectionClosed as e:
            self._log(f"connection closed: {e}")

    async def _stdin_reader(self):
        """Read JSON messages from Emacs via stdin."""
        loop = asyncio.get_event_loop()
        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        await loop.connect_read_pipe(lambda: protocol, sys.stdin)

        while True:
            line = await reader.readline()
            if not line:
                self._log("stdin closed")
                break
            try:
                msg = json.loads(line.decode())
                self._apply_from_emacs(msg)
            except json.JSONDecodeError as e:
                self._log(f"invalid JSON from emacs: {e}")

    async def run(self):
        """Main entry point - connect to Elva and bridge to Emacs."""
        self._log(f"connecting to {self.url}")

        # Observe text changes for Emacs
        self.text.observe(self._on_text_change)

        # Observe doc updates for server
        self.doc.observe(self._on_doc_update)

        try:
            # ping_interval sends pings to detect dead connections
            async with websockets.connect(
                self.url,
                ping_interval=20,
                ping_timeout=10,
            ) as ws:
                self._ws = ws
                self._log("connected")

                # Clear any stale awareness from previous sessions
                # Only keep our own local state
                my_id = self.awareness.client_id
                stale_ids = [cid for cid in self.awareness._states if cid != my_id]
                if stale_ids:
                    self.awareness.remove_awareness_states(stale_ids, origin="local")
                    self._log(f"cleared {len(stale_ids)} stale awareness entries")

                # Initial sync
                await self._send_sync_step1()

                # Also send our current state (proactive cross-sync)
                update = bytes(self.doc.get_update(b"\x00"))
                if update != b"\x00\x00":
                    msg = encode_message((SYNC, SYNC_STEP2), update)
                    await ws.send(msg)
                    self._log("sent proactive sync step 2")

                # Send initial awareness
                client_ids = [self.awareness.client_id]
                payload = self.awareness.encode_awareness_update(client_ids)
                msg = encode_message((AWARENESS,), bytes(payload))
                await ws.send(msg)
                self._log("sent initial awareness")

                try:
                    # Run all tasks concurrently
                    await asyncio.gather(
                        self._ws_receiver(),
                        self._ws_sender(),
                        self._awareness_sender(),
                        self._stdin_reader(),
                    )
                finally:
                    # Send awareness disconnect (set local state to None)
                    try:
                        self.awareness.set_local_state(None)
                        client_ids = [self.awareness.client_id]
                        payload = self.awareness.encode_awareness_update(client_ids)
                        msg = encode_message((AWARENESS,), bytes(payload))
                        await ws.send(msg)
                        self._log("sent awareness disconnect")
                    except Exception:
                        pass  # Connection may already be closed
        except websockets.exceptions.InvalidStatus as e:
            # HTTP error from server - use the server's reason phrase
            status_code = e.response.status_code
            reason = e.response.reason_phrase
            error_msg = f"Server error: HTTP {status_code} {reason}"
            self._log(error_msg)
            self._send_to_emacs({"op": "error", "message": error_msg})
            raise
        except websockets.exceptions.InvalidURI as e:
            error_msg = f"Invalid URL: {e}"
            self._log(error_msg)
            self._send_to_emacs({"op": "error", "message": error_msg})
            raise
        except OSError as e:
            error_msg = f"Connection failed: {e}"
            self._log(error_msg)
            self._send_to_emacs({"op": "error", "message": error_msg})
            raise
        except Exception as e:
            self._log(f"error: {e}")
            raise


async def main():
    if len(sys.argv) < 2:
        print("Usage: elva-bridge.py <websocket-url>", file=sys.stderr)
        print("Example: elva-bridge.py ws://localhost:8000/my-room-id", file=sys.stderr)
        sys.exit(1)

    url = sys.argv[1]
    bridge = ElvaBridge(url)
    await bridge.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        print(f"[bridge] FATAL: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
        import traceback
        traceback.print_exc()
        sys.exit(1)
