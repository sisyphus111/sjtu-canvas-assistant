"""Thin Canvas API client for SJTU Canvas."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Type, TypeVar, Union

import httpx
from pydantic import TypeAdapter

from .exceptions import (
    CanvasAPIError,
    CanvasAuthError,
    CanvasNetworkError,
    CanvasNotFoundError,
    CanvasPermissionError,
    DownloadError,
)
from .models import Assignment, CanvasFile, Course, Folder, Module, ModuleItem, User

ParamInput = Optional[
    Union[Mapping[str, Any], Sequence[Tuple[str, Any]]]
]
T = TypeVar("T")


class CanvasClient:
    """Canvas REST API client with bearer-token auth and pagination."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout_seconds: float = 30.0,
        http_client: Optional[httpx.Client] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self._owns_client = http_client is None
        self._client = http_client or httpx.Client(
            timeout=httpx.Timeout(timeout_seconds),
            follow_redirects=True,
            headers={"User-Agent": "sjtu-canvas-assistant/0.1"},
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "CanvasClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _absolute_url(self, path_or_url: str) -> str:
        if path_or_url.startswith(("http://", "https://")):
            return path_or_url
        if not path_or_url.startswith("/"):
            path_or_url = "/" + path_or_url
        return f"{self.base_url}{path_or_url}"

    def _headers(self, headers: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
        merged = {"Authorization": f"Bearer {self.token}"}
        if headers:
            merged.update(headers)
        return merged

    def _request(
        self,
        method: str,
        path_or_url: str,
        *,
        params: ParamInput = None,
        headers: Optional[Mapping[str, str]] = None,
        stream: bool = False,
    ) -> httpx.Response:
        url = self._absolute_url(path_or_url)
        try:
            if stream:
                request = self._client.build_request(
                    method, url, params=params, headers=self._headers(headers)
                )
                response = self._client.send(request, stream=True, follow_redirects=True)
                try:
                    self._raise_for_status(response)
                except Exception:
                    response.close()
                    raise
                return response
            response = self._client.request(
                method, url, params=params, headers=self._headers(headers)
            )
        except httpx.TimeoutException as exc:
            raise CanvasNetworkError(f"Canvas request timed out: {method} {url}") from exc
        except httpx.TransportError as exc:
            raise CanvasNetworkError(f"Canvas network failure: {method} {url}") from exc

        self._raise_for_status(response)
        return response

    def _raise_for_status(self, response: httpx.Response) -> None:
        if response.status_code < 400:
            return
        message = self._safe_error_message(response)
        if response.status_code == 401:
            raise CanvasAuthError(401, "Canvas rejected the token. Check CANVAS_TOKEN.")
        if response.status_code == 403:
            raise CanvasPermissionError(403, f"Canvas denied access: {message}")
        if response.status_code == 404:
            raise CanvasNotFoundError(404, f"Canvas resource not found: {message}")
        raise CanvasAPIError(response.status_code, f"Canvas API error {response.status_code}: {message}")

    @staticmethod
    def _safe_error_message(response: httpx.Response) -> str:
        try:
            payload = response.json()
        except json.JSONDecodeError:
            text = response.text.strip()
            return text[:300] if text else response.reason_phrase

        if isinstance(payload, dict):
            for key in ("message", "error", "errors"):
                value = payload.get(key)
                if value:
                    return str(value)[:300]
        return response.reason_phrase

    @staticmethod
    def _normalize_params(params: ParamInput = None) -> List[Tuple[str, Any]]:
        if params is None:
            return []
        if isinstance(params, Mapping):
            normalized: List[Tuple[str, Any]] = []
            for key, value in params.items():
                if isinstance(value, (list, tuple)):
                    normalized.extend((key, item) for item in value)
                else:
                    normalized.append((key, value))
            return normalized
        return list(params)

    def _get_json(self, path: str, *, params: ParamInput = None) -> Any:
        response = self._request("GET", path, params=params)
        try:
            return response.json()
        except json.JSONDecodeError as exc:
            raise CanvasAPIError(response.status_code, "Canvas returned invalid JSON.") from exc

    def _get_model(self, path: str, model: Type[T], *, params: ParamInput = None) -> T:
        return model.model_validate(self._get_json(path, params=params))  # type: ignore[attr-defined]

    def _get_paginated_json(self, path: str, *, params: ParamInput = None) -> List[Any]:
        per_page = 100
        page = 1
        all_items: List[Any] = []
        next_url: Optional[str] = None
        base_params = self._normalize_params(params)

        while True:
            if next_url:
                response = self._request("GET", next_url)
            else:
                page_params = [*base_params, ("per_page", per_page), ("page", page)]
                response = self._request("GET", path, params=page_params)
            try:
                items = response.json()
            except json.JSONDecodeError as exc:
                raise CanvasAPIError(response.status_code, "Canvas returned invalid JSON.") from exc
            if not isinstance(items, list):
                raise CanvasAPIError(
                    response.status_code,
                    "Canvas paginated endpoint returned a non-list JSON payload.",
                )
            all_items.extend(items)

            link_next = response.links.get("next", {}).get("url")
            if link_next:
                next_url = link_next
                continue
            if len(items) < per_page:
                break
            page += 1

        return all_items

    def _get_paginated_models(
        self, path: str, model: Type[T], *, params: ParamInput = None
    ) -> List[T]:
        adapter = TypeAdapter(List[model])  # type: ignore[valid-type]
        return adapter.validate_python(self._get_paginated_json(path, params=params))

    def get_current_user(self) -> User:
        return self._get_model("/api/v1/users/self", User)

    def list_courses(self, *, active_only: bool = False) -> List[Course]:
        params: List[Tuple[str, Any]] = [
            ("include[]", "teachers"),
            ("include[]", "term"),
        ]
        if active_only:
            params.append(("enrollment_state", "active"))
        courses = self._get_paginated_models("/api/v1/courses", Course, params=params)
        return [course for course in courses if not course.access_restricted_by_date]

    def get_course(self, course_id: int) -> Course:
        return self._get_model(
            f"/api/v1/courses/{course_id}",
            Course,
            params=[("include[]", "teachers"), ("include[]", "term")],
        )

    def list_course_folders(self, course_id: int) -> List[Folder]:
        return self._get_paginated_models(f"/api/v1/courses/{course_id}/folders", Folder)

    def list_folder_files(self, folder_id: int) -> List[CanvasFile]:
        return self._get_paginated_models(f"/api/v1/folders/{folder_id}/files", CanvasFile)

    def list_folder_folders(self, folder_id: int) -> List[Folder]:
        return self._get_paginated_models(f"/api/v1/folders/{folder_id}/folders", Folder)

    def list_course_files(self, course_id: int) -> List[CanvasFile]:
        return self._get_paginated_models(f"/api/v1/courses/{course_id}/files", CanvasFile)

    def list_modules(self, course_id: int, *, include_items: bool = True) -> List[Module]:
        modules = self._get_paginated_models(f"/api/v1/courses/{course_id}/modules", Module)
        if include_items:
            for module in modules:
                module.items = self.list_module_items(course_id, module.id)
        return modules

    def list_module_items(self, course_id: int, module_id: int) -> List[ModuleItem]:
        return self._get_paginated_models(
            f"/api/v1/courses/{course_id}/modules/{module_id}/items", ModuleItem
        )

    def list_assignments(self, course_id: int) -> List[Assignment]:
        params = [
            ("include[]", "submission"),
            ("include[]", "overrides"),
            ("include[]", "all_dates"),
            ("include[]", "score_statistics"),
        ]
        return self._get_paginated_models(
            f"/api/v1/courses/{course_id}/assignments", Assignment, params=params
        )

    def download_file(
        self,
        file: CanvasFile,
        destination: Path,
        *,
        chunk_size: int = 1024 * 1024,
    ) -> None:
        if not file.url:
            raise DownloadError(f"Canvas file {file.id} has no download URL.")
        destination.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = destination.with_name(destination.name + ".part")
        response: Optional[httpx.Response] = None
        try:
            response = self._request("GET", file.url, stream=True)
            with tmp_path.open("wb") as handle:
                for chunk in response.iter_bytes(chunk_size=chunk_size):
                    if chunk:
                        handle.write(chunk)
            tmp_path.replace(destination)
        except Exception:
            if tmp_path.exists():
                tmp_path.unlink()
            raise
        finally:
            if response is not None:
                response.close()

        if file.size and destination.stat().st_size != file.size:
            raise DownloadError(
                f"Downloaded size mismatch for {file.effective_name}: "
                f"expected {file.size}, got {destination.stat().st_size}."
            )
