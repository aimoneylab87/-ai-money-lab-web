import os
import time

from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from openai import OpenAI


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


class AzureSoraVideoProvider(VideoProvider):
    """Azure OpenAI Sora 2 video provider using managed identity."""

    RESOLUTION_MAP = {
        "vertical": "720x1280",
        "landscape": "1280x720",
    }

    SUPPORTED_SECONDS = {4, 8, 12}

    def __init__(self):
        endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT")
        deployment = os.environ.get(
            "AZURE_OPENAI_DEPLOYMENT_NAME",
            "sora-2",
        )

        if not endpoint:
            raise VideoProviderError(
                "AZURE_OPENAI_ENDPOINT is required"
            )

        token_provider = get_bearer_token_provider(
            DefaultAzureCredential(),
            "https://ai.azure.com/.default",
        )

        self.client = OpenAI(
            base_url=f"{endpoint.rstrip('/')}/openai/v1/",
            api_key=token_provider,
        )

        self.deployment = deployment
        self.poll_interval = int(
            os.environ.get("SORA_POLL_INTERVAL_SECONDS", "5")
        )
        self.poll_timeout = int(
            os.environ.get("SORA_POLL_TIMEOUT_SECONDS", "600")
        )

    def _get_size(self, resolution: str) -> str:
        try:
            return self.RESOLUTION_MAP[resolution]
        except KeyError:
            raise VideoProviderError(
                "Sora 2 currently supports vertical and landscape "
                "output in this provider"
            )

    def generate_video(
        self,
        prompt: str,
        duration_seconds: int,
        resolution: str,
    ) -> bytes:
        if not prompt.strip():
            raise VideoProviderError("Prompt is required")

        if duration_seconds not in self.SUPPORTED_SECONDS:
            raise VideoProviderError(
                "Sora 2 duration must be 4, 8, or 12 seconds"
            )

        size = self._get_size(resolution)

        try:
            video = self.client.videos.create(
                model=self.deployment,
                prompt=prompt,
                size=size,
                seconds=duration_seconds,
            )

            print(
                f"SORA_VIDEO_CREATED: "
                f"id={video.id} status={video.status}"
            )

            deadline = time.monotonic() + self.poll_timeout

            while True:
                video = self.client.videos.retrieve(video.id)

                print(
                    f"SORA_VIDEO_STATUS: "
                    f"id={video.id} "
                    f"status={video.status} "
                    f"progress={getattr(video, 'progress', None)}"
                )

                if video.status == "completed":
                    break

                if video.status == "failed":
                    error = getattr(video, "error", None)
                    raise VideoProviderError(
                        f"Sora 2 generation failed: {error}"
                    )

                if time.monotonic() >= deadline:
                    raise VideoProviderError(
                        "Sora 2 generation timed out"
                    )

                time.sleep(self.poll_interval)

            content = self.client.videos.download_content(
                video.id,
                variant="video",
            )

            video_bytes = content.read()

            if not video_bytes:
                raise VideoProviderError(
                    "Sora 2 returned an empty video"
                )

            if len(video_bytes) < 8 or video_bytes[4:8] != b"ftyp":
                raise VideoProviderError(
                    "Sora 2 response does not appear to be a valid MP4"
                )

            print(
                f"SORA_VIDEO_DOWNLOADED: "
                f"id={video.id} bytes={len(video_bytes)}"
            )

            return video_bytes

        except VideoProviderError:
            raise
        except Exception as exc:
            raise VideoProviderError(
                f"Sora 2 request failed: "
                f"{type(exc).__name__}: {exc}"
            ) from exc


def get_video_provider(provider_name: str) -> VideoProvider:
    name = (provider_name or "sora-2").strip().lower()

    if name in {"sora-2", "sora", "azure-sora"}:
        return AzureSoraVideoProvider()

    raise VideoProviderError(
        f"Unsupported video provider: {provider_name}"
    )
