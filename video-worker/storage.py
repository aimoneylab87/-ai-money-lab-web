import os

from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobServiceClient, ContentSettings


class VideoStorage:
    def __init__(self):
        account_name = os.environ["VIDEO_STORAGE_ACCOUNT"]

        self.video_container = os.environ.get(
            "VIDEO_CONTAINER",
            "videos",
        )

        self.thumbnail_container = os.environ.get(
            "THUMBNAIL_CONTAINER",
            "thumbnails",
        )

        account_url = (
            f"https://{account_name}.blob.core.windows.net"
        )

        self.client = BlobServiceClient(
            account_url=account_url,
            credential=DefaultAzureCredential(),
        )

    def upload_video(self, job_id: str, content: bytes) -> str:
        blob_name = f"{job_id}.mp4"

        blob_client = self.client.get_blob_client(
            container=self.video_container,
            blob=blob_name,
        )

        blob_client.upload_blob(
            content,
            overwrite=True,
            content_settings=ContentSettings(
                content_type="video/mp4"
            ),
        )

        return blob_client.url

    def upload_thumbnail(self, job_id: str, content: bytes) -> str:
        blob_name = f"{job_id}.jpg"

        blob_client = self.client.get_blob_client(
            container=self.thumbnail_container,
            blob=blob_name,
        )

        blob_client.upload_blob(
            content,
            overwrite=True,
            content_settings=ContentSettings(
                content_type="image/jpeg"
            ),
        )

        return blob_client.url
