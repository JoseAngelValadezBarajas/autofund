from pathlib import Path

from autofund.dashboard import runtime


def test_launcher_reuses_ready_dashboard_without_spawning_or_browser(monkeypatch) -> None:
    monkeypatch.setattr(runtime, "_ready", lambda: True)
    monkeypatch.setattr(runtime.webbrowser, "open", lambda *_args, **_kwargs: False)
    assert runtime.ensure_dashboard(Path("session"), open_browser=False) == runtime.URL


def test_browser_failure_does_not_block_and_open_is_once(monkeypatch):
    calls = []
    monkeypatch.setattr(runtime, "_ready", lambda: True)
    def fail(*args, **kwargs):
        calls.append(args)
        raise OSError("browser unavailable")
    monkeypatch.setattr(runtime.webbrowser, "open", fail)
    assert runtime.ensure_dashboard(Path("session")) == runtime.URL
    assert len(calls) == 1


def test_owned_cleanup_does_not_touch_reused_server(monkeypatch):
    monkeypatch.setattr(runtime, "_owned", None)
    runtime.close_owned_dashboard()
    class Process:
        terminated = False
        def poll(self): return None
        def terminate(self): self.terminated = True
        def wait(self, timeout): return 0
    process = Process()
    monkeypatch.setattr(runtime, "_owned", process)
    runtime.close_owned_dashboard()
    assert process.terminated and runtime._owned is None


def test_demo_server_is_not_reused_for_real_session(monkeypatch):
    import httpx
    monkeypatch.setattr(runtime.httpx, "get", lambda *a, **k: httpx.Response(200, json={"application":"AutoFund", "ready":True, "demo_mode":True}))
    assert runtime._ready() is False
