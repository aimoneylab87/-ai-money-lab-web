class VideoProviderError(Exception):
    """Raised when a video provider cannot generate a video."""


class VideoProvider:
    """Provider-neutral interface for video generation."""

    def generate_video(
        self,
        prompt: str,
        duration_seconds: int,
        resolution: str,
    ) -> bytes:
        raise NotImplementedError


class MockVideoProvider(VideoProvider):
    """
    Development provider.

    It creates a deterministic placeholder payload so the worker
    pipeline can be tested without calling a paid video API.
    """

    def generate_video(
        self,
        prompt: str,
        duration_seconds: int,
        resolution: str,
    ) -> bytes:
        if not prompt.strip():
            raise VideoProviderError("Prompt is required")

        if duration_seconds < 1 or duration_seconds > 60:
            raise VideoProviderError(
                "duration_seconds must be between 1 and 60"
            )

        if resolution not in {"vertical", "square", "landscape"}:
            raise VideoProviderError(
                "Unsupported resolution"
            )

        payload = (
            "AI Money Lab mock video\n"
            f"prompt={prompt}\n"
            f"duration_seconds={duration_seconds}\n"
            f"resolution={resolution}\n"
        )

        return payload.encode("utf-8")


def get_video_provider(provider_name: str) -> VideoProvider:
    name = (provider_name or "default").strip().lower()

    if name in {"default", "mock"}:
        return MockVideoProvider()

    raise VideoProviderError(
        f"Unsupported video provider: {provider_name}"
    )
