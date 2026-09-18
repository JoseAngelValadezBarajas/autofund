from pathlib import Path

from autofund.dashboard import runtime


def test_launcher_reuses_ready_dashboard_without_spawning_or_browser(monkeypatch) -> None:
    monkeypatch.setattr(runtime, "_ready", lambda: True)
    monkeypatch.setattr(runtime.webbrowser, "open", lambda *_args, **_kwargs: False)
    assert runtime.ensure_dashboard(Path("session"), open_browser=False) == runtime.URL
