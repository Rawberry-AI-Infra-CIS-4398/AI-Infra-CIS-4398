import os
from collections.abc import Iterator

from app.config import Settings, get_settings

try:
    from google import genai
except ModuleNotFoundError:  # pragma: no cover
    genai = None


class GeminiConfigurationError(RuntimeError):
    """Real chat was requested without the configuration it requires."""


class MockChatClient:
    def generate_reply(self, message: str, *, system_instruction: str | None = None) -> str:
        # Accept the same configuration as Gemini; echo content without simulating preferences.
        if "Use the following document context" in message:
            return message
        return f"You said: {message}"

    def stream_reply(self, message: str, *, system_instruction: str | None = None) -> Iterator[str]:
        reply = self.generate_reply(message, system_instruction=system_instruction)
        for offset in range(0, len(reply), 24):
            yield reply[offset:offset + 24]

# This class handles communication with Gemini through Vertex AI.
# Currently, a new Google GenAI client is created for each generate_reply()
# call. In the future, the client could be initialized once and reused.
class GeminiChatClient:
    def __init__(self, settings: Settings | None = None) -> None:
        # Store the settings used by the Gemini client.
        self.settings = settings or get_settings()
        if not self.settings.use_mock_chat and not self._get_project_id():
            raise GeminiConfigurationError(
                'USE_MOCK_CHAT=false requires a Google Cloud project. Set GOOGLE_CLOUD_PROJECT '
                '(or GCLOUD_PROJECT / GOOGLE_CLOUD_PROJECT_ID), or explicitly enable '
                'USE_MOCK_CHAT=true for local development.'
            )

    def _get_project_id(self) -> str | None:
        # Find the Google Cloud project ID in settings or environment variables.
        return next((value.strip() for value in (
            self.settings.google_cloud_project, os.getenv("GOOGLE_CLOUD_PROJECT"),
            os.getenv("GCLOUD_PROJECT"), os.getenv("GOOGLE_CLOUD_PROJECT_ID"),
        ) if value and value.strip()), None)

    def should_use_real_client(self) -> bool:
        # Only an explicit mock setting permits echo responses.
        return not self.settings.use_mock_chat

    def generate_reply(self, message: str, *, system_instruction: str | None = None) -> str:
        # Use mock responses only when mock mode is explicitly configured.
        if not self.should_use_real_client():
            return MockChatClient().generate_reply(message, system_instruction=system_instruction)

        if genai is None:
            raise RuntimeError("google-genai is not installed. Install it from requirements.txt.")

        project = self._get_project_id()
        if not project:
            raise RuntimeError(
                "Vertex AI project is not configured. Set GOOGLE_CLOUD_PROJECT before enabling real Gemini calls."
            )

        location = (
            self.settings.google_cloud_location
            or os.getenv("GOOGLE_CLOUD_LOCATION")
            or "global"
        )

        with genai.Client(
            vertexai=True,
            project=project,
            location=location,
        ) as client:
            response = client.models.generate_content(
                model=self.settings.gemini_model,
                contents=message,
                config={"system_instruction": system_instruction} if system_instruction else None,
            )
            text = getattr(response, "text", None)
            if not isinstance(text, str) or not text.strip():
                raise RuntimeError('Gemini returned no answer text.')
            return text.strip()

    def stream_reply(self, message: str, *, system_instruction: str | None = None) -> Iterator[str]:
        """Forward provider text chunks as they arrive, preserving whitespace."""
        if not self.should_use_real_client():
            yield from MockChatClient().stream_reply(message, system_instruction=system_instruction)
            return
        if genai is None:
            raise RuntimeError("google-genai is not installed. Install it from requirements.txt.")

        with genai.Client(
            vertexai=True,
            project=self._get_project_id(),
            location=self.settings.google_cloud_location or "global",
        ) as client:
            stream = client.models.generate_content_stream(
                model=self.settings.gemini_model,
                contents=message,
                config={"system_instruction": system_instruction} if system_instruction else None,
            )
            try:
                for chunk in stream:
                    if chunk.text:
                        yield chunk.text
            finally:
                close = getattr(stream, 'close', None)
                if close is not None:
                    close()
