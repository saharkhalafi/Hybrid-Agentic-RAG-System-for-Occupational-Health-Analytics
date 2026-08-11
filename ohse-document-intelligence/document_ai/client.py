"""Google Document AI client — authenticates via ADC, service account, or REST fallback."""

from __future__ import annotations

import base64
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import requests
from google.api_core.client_options import ClientOptions
from google.api_core.exceptions import GoogleAPICallError
from google.cloud import documentai_v1 as documentai
from google.oauth2 import service_account
from tenacity import retry, stop_after_attempt, wait_exponential

from config.logging import get_logger
from config.settings import Settings, get_settings

logger = get_logger(__name__)


class DocumentAIClient:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

        self.processor_name = self.settings.document_ai_processor_name
        self.document_ai_location = self._extract_processor_location(
            self.processor_name
        )

        logger.info(
            "document_ai_client_initialized",
            processor=self.processor_name,
            location=self.document_ai_location,
            endpoint=f"https://{self.document_ai_location}-documentai.googleapis.com",
        )

        self._client = self._build_client()

    @staticmethod
    def _extract_processor_location(processor_name: str) -> str:
        """
        Extract Document AI location from processor resource name.

        Example:
        projects/859603513624/locations/us/processors/abc123

        -> us
        """
        match = re.search(
            r"projects/[^/]+/locations/([^/]+)/processors/[^/]+$",
            processor_name,
        )

        if not match:
            raise ValueError(
                f"Invalid Document AI processor resource name: {processor_name}"
            )

        return match.group(1)

    @property
    def _api_endpoint(self) -> str:
        return f"{self.document_ai_location}-documentai.googleapis.com"

    def _build_client(self) -> documentai.DocumentProcessorServiceClient:
        client_options = ClientOptions(
            api_endpoint=self._api_endpoint,
        )

        credentials_path = self.settings.google_application_credentials

        if credentials_path and Path(credentials_path).exists():
            credentials = service_account.Credentials.from_service_account_file(
                str(credentials_path),
                scopes=["https://www.googleapis.com/auth/cloud-platform"],
            )

            return documentai.DocumentProcessorServiceClient(
                client_options=client_options,
                credentials=credentials,
            )

        return documentai.DocumentProcessorServiceClient(
            client_options=client_options,
        )

    def _gcloud_access_token(self) -> str | None:
        gcloud = shutil.which("gcloud") or shutil.which("gcloud.cmd")

        if not gcloud:
            return None

        try:
            return subprocess.check_output(
                [gcloud, "auth", "print-access-token"],
                text=True,
            ).strip()
        except subprocess.CalledProcessError:
            return None

    def _process_via_rest(
        self,
        content: bytes,
        mime_type: str,
    ) -> dict[str, Any]:

        token = self._gcloud_access_token()

        if not token:
            raise RuntimeError(
                "Document AI REST fallback requires gcloud CLI authentication"
            )

        url = (
            f"https://{self.document_ai_location}-documentai.googleapis.com/v1/"
            f"{self.processor_name}:process"
        )

        logger.info(
            "document_ai_rest_request",
            endpoint=self._api_endpoint,
            processor=self.processor_name,
            bytes=len(content),
        )

        payload = {
            "rawDocument": {
                "content": base64.b64encode(content).decode("ascii"),
                "mimeType": mime_type,
            }
        }

        response = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=300,
        )

        response.raise_for_status()

        data = response.json()

        return data.get("document", data)

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(
            multiplier=1,
            min=2,
            max=10,
        ),
    )
    def process_document(
        self,
        content: bytes,
        mime_type: str = "application/pdf",
    ) -> documentai.Document | dict[str, Any]:

        request = documentai.ProcessRequest(
            name=self.processor_name,
            raw_document=documentai.RawDocument(
                content=content,
                mime_type=mime_type,
            ),
        )

        logger.info(
            "document_ai_request",
            processor=self.processor_name,
            location=self.document_ai_location,
            endpoint=self._api_endpoint,
            bytes=len(content),
        )

        try:
            result = self._client.process_document(request=request)

            logger.info(
                "document_ai_grpc_success",
                processor=self.processor_name,
            )

            return result.document

        except GoogleAPICallError as exc:
            logger.warning(
                "document_ai_grpc_failed_using_rest_fallback",
                processor=self.processor_name,
                endpoint=self._api_endpoint,
                error=str(exc),
            )

            return self._process_via_rest(
                content,
                mime_type,
            )

    @staticmethod
    def document_to_dict(
        document: documentai.Document | dict[str, Any],
    ) -> dict[str, Any]:

        if isinstance(document, dict):
            return document

        return json.loads(
            documentai.Document.to_json(document)
        )