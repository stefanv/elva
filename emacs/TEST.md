# Testing the Emacs Client

## Start the Server

```bash
.venv/bin/elva server --port 46795
```

## In Emacs

```elisp
;; Load the package
(load-file "/home/stefan/src/elva/emacs/elva.el")

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
