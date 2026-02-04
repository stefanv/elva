# Testing the Emacs Client

## Start the Server

```bash
.venv/bin/elva server --port 46795
```

## In Emacs

```elisp
;; Load the package (adjust path as needed)
(load-file "~/src/elva/emacs/elva.el")

;; Set Python to use the venv (required for pycrdt/websockets)
(setq elva-bridge-program "~/src/elva/.venv/bin/python3")

;; Connect a buffer
M-x elva-connect RET ws://localhost:46795/your-room-id RET
```

## Test with Elva Editor (second client)

```bash
.venv/bin/elva editor --host localhost --port 46795 --identifier your-room-id
```

## Useful Commands

- `M-x elva-status` - check connection
- `M-x elva-disconnect` - disconnect

## Server API

### List Rooms

Using the CLI:
```bash
.venv/bin/elva rooms --host localhost --port 46795
```

Or via HTTP:
```bash
curl http://localhost:46795/rooms
```

Returns JSON with active rooms:
```json
{
  "rooms": [
    {"identifier": "room-id-here", "clients": 2, "persistent": true}
  ],
  "count": 1
}
```

## Testing Scenarios

### Basic sync
1. Start server
2. Connect Emacs buffer
3. Connect Elva editor with same room ID
4. Type in one, verify it appears in the other

### Multi-byte characters
1. Type emoji or CJK characters in Emacs
2. Verify they appear correctly in Elva editor
3. Type in Elva editor, verify in Emacs

### Reconnection
1. Connect Emacs to server
2. Kill the server
3. Observe reconnection attempts in Emacs
4. Restart server
5. Verify reconnection succeeds
