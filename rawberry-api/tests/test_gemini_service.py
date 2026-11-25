from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.config import Settings
from app.services.chat_service import ChatService
from app.services.gemini_service import GeminiChatClient, GeminiConfigurationError, MockChatClient
from app.store import InMemoryStore
from app.user_models import ChatRequest
from app.main import create_app
from fastapi.testclient import TestClient


@pytest.fixture
def no_project(monkeypatch):
    for key in ['GOOGLE_CLOUD_PROJECT', 'GCLOUD_PROJECT', 'GOOGLE_CLOUD_PROJECT_ID']:
        monkeypatch.delenv(key, raising=False)


@pytest.mark.parametrize('project', [None, '', '   '])
def test_real_chat_without_project_raises_configuration_error(no_project, project):
    settings = Settings(use_mock_chat=False, google_cloud_project=project)
    with pytest.raises(GeminiConfigurationError, match='USE_MOCK_CHAT=false requires a Google Cloud project'):
        GeminiChatClient(settings)
    with pytest.raises(GeminiConfigurationError):
        ChatService(InMemoryStore(), settings=settings)


def test_api_refuses_startup_when_real_chat_has_no_project(no_project, monkeypatch):
    monkeypatch.setenv('USE_MOCK_CHAT', 'false')
    with pytest.raises(GeminiConfigurationError, match='GOOGLE_CLOUD_PROJECT'):
        with TestClient(create_app(store=InMemoryStore())):
            pytest.fail('Misconfigured real-chat server must not start')


@pytest.mark.parametrize('key', ['GOOGLE_CLOUD_PROJECT', 'GCLOUD_PROJECT', 'GOOGLE_CLOUD_PROJECT_ID'])
def test_real_chat_accepts_supported_project_environment_names(no_project, monkeypatch, key):
    monkeypatch.setenv(key, 'demo-project')
    client = GeminiChatClient(Settings(use_mock_chat=False))
    assert client.should_use_real_client() is True
    assert client._get_project_id() == 'demo-project'


def test_explicit_mock_mode_without_project_still_starts_and_streams(no_project, monkeypatch):
    monkeypatch.setenv('USE_MOCK_CHAT', 'true')
    client = GeminiChatClient(Settings(use_mock_chat=True))
    assert client.should_use_real_client() is False
    assert ''.join(client.stream_reply('hello')) == 'You said: hello'
    with TestClient(create_app(store=InMemoryStore())) as api:
        assert api.get('/health').status_code == 200


@pytest.mark.parametrize('text', [None, '', '   '])
def test_real_response_without_text_does_not_become_mock(monkeypatch, text):
    sdk = MagicMock()
    sdk.__enter__.return_value = sdk
    sdk.models.generate_content.return_value = SimpleNamespace(text=text)
    monkeypatch.setattr('app.services.gemini_service.genai', SimpleNamespace(Client=MagicMock(return_value=sdk)))
    client = GeminiChatClient(Settings(google_cloud_project='demo-project', use_mock_chat=False))
    with pytest.raises(RuntimeError, match='Gemini returned no answer text'):
        client.generate_reply('hello')


@pytest.mark.parametrize('streaming', [False, True])
def test_real_provider_errors_are_not_mocked(monkeypatch, streaming):
    sdk = MagicMock()
    sdk.__enter__.return_value = sdk
    sdk.models.generate_content.side_effect = RuntimeError('Provider unavailable')
    sdk.models.generate_content_stream.side_effect = RuntimeError('Provider unavailable')
    monkeypatch.setattr('app.services.gemini_service.genai', SimpleNamespace(Client=MagicMock(return_value=sdk)))
    client = GeminiChatClient(Settings(google_cloud_project='demo-project', use_mock_chat=False))
    with pytest.raises(RuntimeError, match='Provider unavailable'):
        if streaming:
            list(client.stream_reply('hello'))
        else:
            client.generate_reply('hello')


def test_gemini_client_falls_back_to_mock_response_when_disabled():
    settings = Settings(google_cloud_project=None, use_mock_chat=True)
    client = GeminiChatClient(settings)

    assert client.generate_reply("hello") == "You said: hello"


@pytest.mark.parametrize('instruction', [None, 'Keep answers concise.'])
def test_gemini_client_uses_real_client_when_enabled(monkeypatch, instruction):
    settings = Settings(google_cloud_project="demo-project", use_mock_chat=False)

    fake_client = MagicMock()
    fake_client.__enter__.return_value = fake_client
    fake_response = MagicMock()
    fake_response.text = "hi from gemini"
    fake_client.models.generate_content.return_value = fake_response

    monkeypatch.setattr(
        "app.services.gemini_service.genai",
        SimpleNamespace(Client=MagicMock(return_value=fake_client)),
    )

    client = GeminiChatClient(settings)
    assert client.generate_reply("hello", system_instruction=instruction) == "hi from gemini"
    fake_client.models.generate_content.assert_called_once_with(
        model=settings.gemini_model,
        contents='hello',
        config={'system_instruction': instruction} if instruction else None,
    )


def test_chat_service_uses_mock_client_by_default():
    settings = Settings(google_cloud_project=None, use_mock_chat=True)
    store = InMemoryStore()

    service = ChatService(store, settings=settings)
    conversation = store.create_conversation("alice", "Test")
    response = service.generate_reply("alice", ChatRequest(conversation_id=conversation["id"], message="hello")).reply

    assert response == "You said: hello"
    assert isinstance(service.llm_client, MockChatClient)


def test_chat_service_uses_gemini_when_available():
    store = InMemoryStore()
    fake_llm = MagicMock()
    fake_llm.generate_reply.return_value = "hello from gemini"

    service = ChatService(store, llm_client=fake_llm)
    conversation = store.create_conversation("alice", "Test")
    response = service.generate_reply("alice", ChatRequest(conversation_id=conversation["id"], message="hello")).reply

    assert response == "hello from gemini"
