"""NTS_075 L2 — smart cover-image scene prompt.

Covers:
* ``build_scene_prompt`` returns a cleaned scene from the LLM (mocked).
* graceful fallback to ``None`` when the LLM errors or no API key is set.
* ``ImageGenerator.generate`` uses ``{scene}, {style}`` when a scene is given
  and falls back to ``{title}, {style}`` when it is not.
* ``generate_image_for_topic`` builds the scene exactly ONCE per topic and
  passes it to the generator (one cover per topic, NTS_069 preserved).
"""

from __future__ import annotations

import asyncio
import types

import pytest

from pipeline.common import config as config_module
from pipeline.common.models import RawItem, Topic
from pipeline.generator import image_prompt as ip
from pipeline.generator.image import BrandVisual, ImageGenerator


def _topic(title: str = "Headline", summary: str = "") -> Topic:
    return Topic(
        id="topic-1",
        brand_id="icon",
        raw=RawItem(
            source_id="s",
            source_name="s",
            url="https://test.example.com/a",
            title=title,
            summary=summary,
        ),
        relevance_score=9.0,
    )


def _fake_openai(content: str | None, *, exc: Exception | None = None):
    """Return a factory standing in for ``openai.AsyncOpenAI``."""

    class _Completions:
        async def create(self, **kw):  # noqa: ANN003
            _fake_openai.last_kwargs = kw
            if exc is not None:
                raise exc
            msg = types.SimpleNamespace(content=content)
            return types.SimpleNamespace(
                choices=[types.SimpleNamespace(message=msg)],
                usage=types.SimpleNamespace(prompt_tokens=40, completion_tokens=15),
            )

    class _Client:
        def __init__(self, *a, **k) -> None:
            self.chat = types.SimpleNamespace(completions=_Completions())

    return _Client


@pytest.fixture
def with_openai_key(monkeypatch):
    monkeypatch.setattr(config_module, "_settings", None)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    yield
    monkeypatch.setattr(config_module, "_settings", None)


def test_build_scene_prompt_returns_cleaned_scene(with_openai_key, monkeypatch):
    import openai

    monkeypatch.setattr(
        openai,
        "AsyncOpenAI",
        _fake_openai('  "A quiet glass tower at dawn."\n'),
    )
    scene = asyncio.run(ip.build_scene_prompt("EU credit fund rules", "Summary here"))
    # Wrapping quotes/whitespace stripped, single line.
    assert scene == "A quiet glass tower at dawn."
    # The article context reached the model.
    msgs = _fake_openai.last_kwargs["messages"]
    assert any("EU credit fund rules" in m["content"] for m in msgs)
    assert any("Summary here" in m["content"] for m in msgs)


def test_build_scene_prompt_none_on_llm_error(with_openai_key, monkeypatch):
    import openai

    monkeypatch.setattr(
        openai, "AsyncOpenAI", _fake_openai(None, exc=RuntimeError("boom"))
    )
    assert asyncio.run(ip.build_scene_prompt("Anything")) is None


def test_build_scene_prompt_none_on_empty_output(with_openai_key, monkeypatch):
    import openai

    monkeypatch.setattr(openai, "AsyncOpenAI", _fake_openai("   "))
    assert asyncio.run(ip.build_scene_prompt("Anything")) is None


def test_build_scene_prompt_none_without_api_key(monkeypatch):
    monkeypatch.setattr(config_module, "_settings", None)
    monkeypatch.setenv("OPENAI_API_KEY", "")
    assert asyncio.run(ip.build_scene_prompt("Anything")) is None


# --- image_prompt prompt_type wired to the prompts DB table ---------------


@pytest.fixture
def admin_db_with_brand(tmp_path, monkeypatch):
    from cryptography.fernet import Fernet

    from pipeline.admin import db as admin_db
    from pipeline.admin import encryption as enc_mod
    from pipeline.admin.models import Prompt
    from tests.unit.conftest import seed_icon_brand

    monkeypatch.setattr(config_module, "_settings", None)
    monkeypatch.setenv("ADMIN_DB_PATH", str(tmp_path / "admin.db"))
    monkeypatch.setenv("BRANDS_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
    enc_mod.reset_for_tests()
    admin_db.reset_for_tests()
    engine = admin_db.get_engine(path=tmp_path / "admin.db")
    admin_db.Base.metadata.create_all(engine)
    with admin_db.get_session_factory()() as session:
        icon_id = seed_icon_brand(session)
        session.commit()
    yield icon_id, admin_db, Prompt
    admin_db.reset_for_tests()
    enc_mod.reset_for_tests()


def _add_image_prompt(admin_db, Prompt, brand_id, content):
    with admin_db.get_session_factory()() as session:
        session.add(
            Prompt(
                brand_id_fk=brand_id,
                prompt_type="image_prompt",
                version_name="v1",
                content=content,
                is_active=True,
            )
        )
        session.commit()


def test_resolve_template_default_when_no_brand():
    assert ip._resolve_image_template(None) == ip._DEFAULT_IMAGE_PROMPT_TEMPLATE


def test_resolve_template_uses_active_db_row(admin_db_with_brand):
    icon_id, admin_db, Prompt = admin_db_with_brand
    custom = "Custom brief for {title} / {summary}"
    _add_image_prompt(admin_db, Prompt, icon_id, custom)
    assert ip._resolve_image_template(icon_id) == custom


def test_resolve_template_rejects_drifted_placeholders(admin_db_with_brand):
    icon_id, admin_db, Prompt = admin_db_with_brand
    # References an unknown placeholder → unsafe → falls back to default.
    _add_image_prompt(admin_db, Prompt, icon_id, "Brief {title} {unknown_field}")
    assert ip._resolve_image_template(icon_id) == ip._DEFAULT_IMAGE_PROMPT_TEMPLATE


def test_resolve_template_default_when_no_active_row(admin_db_with_brand):
    icon_id, _admin_db, _Prompt = admin_db_with_brand
    assert ip._resolve_image_template(icon_id) == ip._DEFAULT_IMAGE_PROMPT_TEMPLATE


def _generator_with_fake_replicate():
    gen = ImageGenerator()

    class _FakeReplicate:
        def __init__(self) -> None:
            self.last_input: dict | None = None

        async def async_run(self, model, input):  # noqa: A002, ANN001
            self.last_input = input
            return "https://replicate.test/master.png"

    gen._client = _FakeReplicate()
    return gen


def test_image_generator_uses_scene_when_present():
    visual = BrandVisual(brand_id="icon", image_style_prompts=["STYLE-X"])
    gen = _generator_with_fake_replicate()
    asyncio.run(gen.generate(_topic(title="Bare headline"), visual, scene="vivid scene"))
    assert gen._client.last_input["prompt"] == "vivid scene, STYLE-X"


def test_image_generator_falls_back_to_title_without_scene():
    visual = BrandVisual(brand_id="icon", image_style_prompts=["STYLE-X"])
    gen = _generator_with_fake_replicate()
    asyncio.run(gen.generate(_topic(title="Bare headline"), visual, scene=None))
    assert gen._client.last_input["prompt"] == "Bare headline, STYLE-X"


def test_generate_image_for_topic_builds_scene_once(monkeypatch):
    """The scene is built once per topic and threaded into the generator —
    proving the LLM call is not multiplied across language siblings."""
    from pipeline import run as pipe

    monkeypatch.setattr(config_module, "_settings", None)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    scene_calls: list[str] = []

    async def fake_scene(title, summary="", *, brand_id_fk=None):  # noqa: ANN001
        scene_calls.append(title)
        return "SCENE"

    captured: dict = {}

    class _FakeGen:
        def __init__(self, *a, **k) -> None:
            pass

        async def generate(self, topic, visual, *, operation="image_master", scene=None):  # noqa: ANN001
            captured["scene"] = scene
            return "https://replicate.test/m.png"

    async def fake_fetch_master(url):  # noqa: ANN001
        return b"png"

    def fake_resize(data, channel):  # noqa: ANN001
        return b"resized"

    monkeypatch.setattr(pipe, "build_scene_prompt", fake_scene)
    monkeypatch.setattr(pipe, "ImageGenerator", _FakeGen)
    monkeypatch.setattr(pipe, "fetch_master", fake_fetch_master)
    monkeypatch.setattr(pipe, "resize_for_channel", fake_resize)

    brand = types.SimpleNamespace(
        slug="icon",
        id_fk=None,
        visual=BrandVisual(brand_id="icon", image_style_prompts=["S"]),
    )

    class _FakePublisher:
        async def upload_cover_image(self, image_bytes, filename):  # noqa: ANN001
            return "asset-xyz"

    asset = asyncio.run(
        pipe.generate_image_for_topic(_topic(), brand, _FakePublisher())
    )
    assert asset == "asset-xyz"
    assert scene_calls == ["Headline"]  # exactly once
    assert captured["scene"] == "SCENE"
