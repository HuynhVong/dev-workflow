"""Start the local server and open the browser with the one-time token link."""
import os
import webbrowser
from pathlib import Path

from .server import App, create_app


def serve(workspace: str | None = None, port: int = 0, open_browser: bool = True, host: str = "127.0.0.1",
          session_factory=None, token: str | None = None) -> None:
    import uvicorn
    from ..jira_implement.runner import Session, load
    path = str(Path(workspace or os.getenv("DEVFLOW_WORKSPACE", "workspace.yaml")).expanduser().resolve())
    if not port:
        try:
            from ..jira_implement.workspace import read_raw
            port = int((read_raw(path).get("ui") or {}).get("port") or 8765)
        except Exception:  # noqa: BLE001
            port = 8765
    state = App(session_factory or (lambda: Session(load(path), ask=None, out=open(os.devnull, "w"))), path, token=token, port=port)
    url = f"http://{host}:{port}/?token={state.token}"
    print(f"devflow UI: {url}\n(local only; keep this link private, it carries your access token)", flush=True)
    if open_browser:
        webbrowser.open(url)
    uvicorn.run(create_app(state), host=host, port=port, log_level="warning")
