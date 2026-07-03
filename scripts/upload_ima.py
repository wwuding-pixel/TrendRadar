# coding=utf-8
"""Upload the latest Feishu notification Markdown to an IMA knowledge base."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import mimetypes
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, Optional
from urllib.parse import quote

import requests


IMA_API_BASE = "https://ima.qq.com/openapi/wiki/v1"
DEFAULT_KNOWLEDGE_BASE_ID = "FXaTaG6r7qFsGo2QyBjhE5EZPYW0cJGkJSmdGA97od0="
DEFAULT_FOLDER_ID = "folder_7478615184788012"


class ImaUploadError(RuntimeError):
    pass


def _env(name: str, default: str = "") -> str:
    value = os.getenv(name, "").strip()
    return value or default


def _ima_headers(client_id: str, api_key: str) -> Dict[str, str]:
    return {
        "ima-openapi-clientid": client_id,
        "ima-openapi-apikey": api_key,
        "Content-Type": "application/json",
    }


def _post_ima(
    endpoint: str,
    payload: Dict[str, Any],
    client_id: str,
    api_key: str,
    timeout: int = 30,
) -> Dict[str, Any]:
    url = f"{IMA_API_BASE}/{endpoint}"
    response = requests.post(
        url,
        headers=_ima_headers(client_id, api_key),
        json=payload,
        timeout=timeout,
    )
    try:
        data = response.json()
    except ValueError as exc:
        raise ImaUploadError(f"{endpoint} returned non-JSON response: {response.text[:500]}") from exc

    if response.status_code >= 400:
        raise ImaUploadError(f"{endpoint} HTTP {response.status_code}: {data}")

    code = data.get("code")
    if code not in (None, 0):
        raise ImaUploadError(f"{endpoint} failed: {data}")

    return data


def _first_mapping(data: Any, required_keys: Iterable[str]) -> Optional[Dict[str, Any]]:
    if isinstance(data, dict) and all(key in data for key in required_keys):
        return data
    if isinstance(data, dict):
        for value in data.values():
            found = _first_mapping(value, required_keys)
            if found:
                return found
    elif isinstance(data, list):
        for value in data:
            found = _first_mapping(value, required_keys)
            if found:
                return found
    return None


def _extract_media_response(data: Dict[str, Any]) -> tuple[str, Dict[str, Any]]:
    media_holder = _first_mapping(data, ["media_id"])
    credential = _first_mapping(data, ["token", "secret_id", "secret_key", "bucket_name", "region", "cos_key"])
    if not media_holder or not credential:
        raise ImaUploadError(f"create_media response missing media_id/cos_credential: {data}")
    return str(media_holder["media_id"]), credential


def _mime_type(path: Path) -> str:
    guessed, _ = mimetypes.guess_type(path.name)
    return guessed or "text/markdown"


def _file_name(path: Path, title_prefix: str = "") -> str:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    prefix = f"{title_prefix.strip()}_" if title_prefix.strip() else ""
    return f"{prefix}{timestamp}_{path.name}"


def _cos_quote(value: str, safe: str = "") -> str:
    return quote(str(value), safe=safe)


def _cos_authorization(
    *,
    method: str,
    cos_key: str,
    headers: Dict[str, str],
    secret_id: str,
    secret_key: str,
    start_time: int,
    expired_time: int,
) -> str:
    key_time = f"{start_time};{expired_time}"
    sign_key = hmac.new(secret_key.encode("utf-8"), key_time.encode("utf-8"), hashlib.sha1).hexdigest()

    normalized_headers = {
        key.lower(): " ".join(str(value).strip().split())
        for key, value in headers.items()
        if value is not None
    }
    signed_header_names = sorted(normalized_headers)
    header_list = ";".join(signed_header_names)
    http_headers = "&".join(
        f"{_cos_quote(name)}={_cos_quote(normalized_headers[name], safe='-_.~')}"
        for name in signed_header_names
    )

    uri = "/" + quote(cos_key.lstrip("/"), safe="/-_.~")
    http_string = f"{method.lower()}\n{uri}\n\n{http_headers}\n"
    string_to_sign = f"sha1\n{key_time}\n{hashlib.sha1(http_string.encode('utf-8')).hexdigest()}\n"
    signature = hmac.new(sign_key.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha1).hexdigest()

    return (
        "q-sign-algorithm=sha1&"
        f"q-ak={secret_id}&"
        f"q-sign-time={key_time}&"
        f"q-key-time={key_time}&"
        f"q-header-list={header_list}&"
        "q-url-param-list=&"
        f"q-signature={signature}"
    )


def _put_cos(file_path: Path, credential: Dict[str, Any], content_type: str) -> None:
    bucket = credential["bucket_name"]
    region = credential["region"]
    cos_key = credential["cos_key"]
    token = credential["token"]
    secret_id = credential["secret_id"]
    secret_key = credential["secret_key"]
    start_time = int(credential["start_time"])
    expired_time = int(credential["expired_time"])

    data = file_path.read_bytes()
    host = f"{bucket}.cos.{region}.myqcloud.com"
    headers = {
        "Content-Type": content_type,
        "Content-Length": str(len(data)),
        "Host": host,
        "x-cos-security-token": token,
    }
    headers["Authorization"] = _cos_authorization(
        method="PUT",
        cos_key=cos_key,
        headers=headers,
        secret_id=secret_id,
        secret_key=secret_key,
        start_time=start_time,
        expired_time=expired_time,
    )

    url = f"https://{host}/{quote(cos_key.lstrip('/'), safe='/-_.~')}"
    response = requests.put(url, headers=headers, data=data, timeout=120)
    if response.status_code not in (200, 201):
        raise ImaUploadError(f"COS upload failed HTTP {response.status_code}: {response.text[:500]}")


def upload_file(
    file_path: Path,
    *,
    client_id: str,
    api_key: str,
    knowledge_base_id: str,
    folder_id: str,
    title_prefix: str,
) -> None:
    if not file_path.exists():
        print(f"IMA upload skipped: file not found: {file_path}")
        return

    file_size = file_path.stat().st_size
    upload_name = _file_name(file_path, title_prefix=title_prefix)
    file_ext = file_path.suffix.lstrip(".").lower() or "md"
    content_type = _mime_type(file_path)

    repeated_payload = {
        "knowledge_base_id": knowledge_base_id,
        "file_names": [upload_name],
    }
    if folder_id:
        repeated_payload["folder_id"] = folder_id
    try:
        repeated = _post_ima("check_repeated_names", repeated_payload, client_id, api_key)
        print(f"IMA repeated-name check completed: {json.dumps(repeated, ensure_ascii=False)[:500]}")
    except Exception as exc:
        print(f"IMA repeated-name check skipped: {exc}")

    create_payload = {
        "file_name": upload_name,
        "file_size": file_size,
        "content_type": content_type,
        "knowledge_base_id": knowledge_base_id,
        "file_ext": file_ext,
    }
    if folder_id:
        create_payload["folder_id"] = folder_id

    create_response = _post_ima("create_media", create_payload, client_id, api_key)
    media_id, credential = _extract_media_response(create_response)
    print(f"IMA media created: media_id={media_id}, file={upload_name}, size={file_size}")

    _put_cos(file_path, credential, content_type)
    print("IMA COS upload completed")

    add_payload = {
        "media_type": 1,
        "media_id": media_id,
        "title": upload_name,
        "knowledge_base_id": knowledge_base_id,
        "file_info": {
            "cos_key": credential["cos_key"],
            "file_size": file_size,
            "file_name": upload_name,
        },
    }
    if folder_id:
        add_payload["folder_id"] = folder_id

    add_response = _post_ima("add_knowledge", add_payload, client_id, api_key)
    print(f"IMA add_knowledge completed: {json.dumps(add_response, ensure_ascii=False)[:500]}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Upload a notification Markdown file to IMA.")
    parser.add_argument("--file", default="output/notifications/latest/feishu.md")
    parser.add_argument("--title-prefix", default=_env("IMA_TITLE_PREFIX", "TrendRadar"))
    args = parser.parse_args()

    client_id = _env("IMA_CLIENT_ID")
    api_key = _env("IMA_API_KEY")
    knowledge_base_id = _env("IMA_KNOWLEDGE_BASE_ID", DEFAULT_KNOWLEDGE_BASE_ID)
    folder_id = _env("IMA_FOLDER_ID", DEFAULT_FOLDER_ID)

    if not client_id or not api_key:
        print("IMA upload skipped: set IMA_CLIENT_ID and IMA_API_KEY secrets to enable upload")
        return 0

    upload_file(
        Path(args.file),
        client_id=client_id,
        api_key=api_key,
        knowledge_base_id=knowledge_base_id,
        folder_id=folder_id,
        title_prefix=args.title_prefix,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
