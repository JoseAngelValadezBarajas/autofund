from autofund.mvp import app


def test_one_command_binds_loopback_and_opens_browser_once(monkeypatch, tmp_path):
    opened = []
    served = []
    monkeypatch.setattr(app.webbrowser, "open", lambda url: opened.append(url))
    monkeypatch.setattr(app.uvicorn, "run", lambda application, **kwargs: served.append(kwargs))
    class ImmediateTimer:
        def __init__(self, _delay, callback):
            self.callback = callback
        def start(self):
            self.callback()
    monkeypatch.setattr(app.threading, "Timer", ImmediateTimer)
    assert app.main(["--demo", "--artifacts", str(tmp_path)]) == 0
    assert opened == ["http://127.0.0.1:8000"]
    assert served == [{"host": "127.0.0.1", "port": 8000, "log_level": "info"}]


def test_browser_failure_does_not_abort_application(monkeypatch, tmp_path):
    monkeypatch.setattr(app.webbrowser, "open", lambda _url: (_ for _ in ()).throw(OSError()))
    monkeypatch.setattr(app.uvicorn, "run", lambda *_args, **_kwargs: None)
    class ImmediateTimer:
        def __init__(self, _delay, callback):
            self.callback = callback
        def start(self):
            try:
                self.callback()
            except OSError:
                pass
    monkeypatch.setattr(app.threading, "Timer", ImmediateTimer)
    assert app.main(["--demo", "--artifacts", str(tmp_path)]) == 0
