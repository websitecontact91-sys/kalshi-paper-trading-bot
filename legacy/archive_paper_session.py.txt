"""Archive a completed stopped paper session without resetting its state."""
import json
from pathlib import Path

def main():
    state = json.loads(Path("paper_state.json").read_text())
    if state.get("mode") != "STOPPED":
        print("No stopped session to archive")
        return
    history = state.get("history") or []
    if not history or not any(t.get("won") is False for t in history):
        raise SystemExit("STOPPED state lacks settled loss")
    last = history[-1]
    session_id = int(history[0].get("entered_at", 0))
    folder = Path("paper_sessions")
    folder.mkdir(exist_ok=True)
    path = folder / f"stopped-{session_id}.json"
    if path.exists():
        if json.loads(path.read_text()) != state:
            raise SystemExit("Archive collision with different content")
        print(f"Archive already exists: {path}")
        return
    path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
    print(f"Archived stopped session to {path}")

if __name__ == "__main__":
    main()
