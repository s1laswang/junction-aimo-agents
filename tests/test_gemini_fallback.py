"""Model fallback in aimo.gemini.generate (found Sun 05:00: the shared key's daily quota for the default agent
model ran out and live runs failed). A daily-quota 429 must switch to the next model at once, and remember it."""
import types

import pytest

from aimo import gemini

DAILY = ("429 RESOURCE_EXHAUSTED. Quota exceeded for metric: generativelanguage.googleapis.com/"
         "generate_requests_per_model_per_day, limit: 10000, model: gemini-3.8-flash")


class FakeModels:
    def __init__(self, behaviour):
        self.behaviour, self.calls = behaviour, []

    def generate_content(self, model, contents, config=None):
        self.calls.append(model)
        out = self.behaviour[model]
        if isinstance(out, Exception):
            raise out
        return types.SimpleNamespace(text=out, usage_metadata=None)


@pytest.fixture
def fake(monkeypatch, tmp_path):
    def install(behaviour):
        models = FakeModels(behaviour)
        monkeypatch.setattr(gemini, "client", lambda: types.SimpleNamespace(models=models))
        return models
    monkeypatch.setattr(gemini, "LEDGER_DIR", tmp_path)
    monkeypatch.setattr(gemini.time, "sleep", lambda s: None)
    monkeypatch.delenv("GEMINI_FALLBACK_MODELS", raising=False)
    gemini._exhausted.clear()
    yield install
    gemini._exhausted.clear()


def test_daily_quota_switches_model_immediately_and_remembers(fake):
    models = fake({"gemini-3.8-flash": Exception(DAILY), "gemini-3.5-flash": "ok", "gemini-4-hydrogen": "ok"})
    r = gemini.generate("test", "gemini-3.8-flash", "hi")
    assert r.text == "ok" and gemini.last_model() == "gemini-3.5-flash"
    assert models.calls == ["gemini-3.8-flash", "gemini-3.5-flash"]  # one failed try, no backoff loop
    gemini.generate("test", "gemini-3.8-flash", "hi")
    assert models.calls[2:] == ["gemini-3.5-flash"]  # exhausted model is skipped next time


def test_non_quota_error_is_raised_not_masked(fake):
    fake({"gemini-3.8-flash": ValueError("400 INVALID_ARGUMENT bad schema"), "gemini-3.5-flash": "ok"})
    with pytest.raises(ValueError):
        gemini.generate("test", "gemini-3.8-flash", "hi")


def test_all_models_exhausted_raises_the_quota_error(fake):
    fake({m: Exception(DAILY) for m in ("gemini-3.8-flash", "gemini-3.5-flash", "gemini-4-hydrogen")})
    with pytest.raises(Exception, match="RESOURCE_EXHAUSTED"):
        gemini.generate("test", "gemini-3.8-flash", "hi")


def test_requested_model_first_when_healthy(fake):
    models = fake({"gemini-3.8-flash": "ok", "gemini-3.5-flash": "ok"})
    gemini.generate("test", "gemini-3.8-flash", "hi")
    assert models.calls == ["gemini-3.8-flash"] and gemini.last_model() == "gemini-3.8-flash"
