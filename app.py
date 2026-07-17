"""UpTube - YouTube 음원 다운로드 웹앱.

검색어 또는 유튜브 링크를 입력하면 영상 목록을 보여주고,
선택한 영상의 오디오를 mp3로 추출해 영상 제목을 파일명으로 내려준다.
"""

import os
import re
import shutil
import tempfile
import threading
import time

import requests
from flask import (
    Flask,
    Response,
    abort,
    jsonify,
    render_template,
    request,
    stream_with_context,
)
import yt_dlp

app = Flask(__name__)

YOUTUBE_URL_RE = re.compile(
    r"https?://(www\.|m\.|music\.)?(youtube\.com|youtu\.be)/", re.IGNORECASE
)
VIDEO_ID_RE = re.compile(r"[A-Za-z0-9_-]{5,20}")
UNSAFE_FILENAME_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')

SEARCH_LIMIT = 15


def resolve_download_dir() -> str:
    """mp3 저장 폴더. 기본은 안드로이드 Download/uptube (Termux 기준)."""
    env = os.environ.get("UPTUBE_DOWNLOAD_DIR")
    if env:
        return os.path.abspath(os.path.expanduser(env))
    home = os.path.expanduser("~")
    candidates = [
        os.path.join(home, "storage", "downloads"),  # Termux (termux-setup-storage)
        "/storage/emulated/0/Download",  # 안드로이드 직접 경로
        os.path.join(home, "Downloads"),  # 일반 리눅스/맥
        os.path.join(home, "Download"),
    ]
    for base in candidates:
        if os.path.isdir(base):
            return os.path.join(base, "uptube")
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "downloads")


DOWNLOAD_DIR = resolve_download_dir()


def unique_path(directory: str, stem: str, ext: str) -> str:
    """같은 제목이 이미 있으면 '제목 (1).mp3' 식으로 피한다."""
    path = os.path.join(directory, f"{stem}{ext}")
    n = 1
    while os.path.exists(path):
        path = os.path.join(directory, f"{stem} ({n}){ext}")
        n += 1
    return path


def is_youtube_url(query: str) -> bool:
    return bool(YOUTUBE_URL_RE.match(query))


def sanitize_filename(name: str) -> str:
    name = UNSAFE_FILENAME_RE.sub("_", name).strip().strip(".")
    return name[:150] or "audio"


def entry_to_item(entry: dict) -> dict:
    video_id = entry.get("id")
    thumbnails = entry.get("thumbnails") or []
    thumbnail = (
        entry.get("thumbnail")
        or (thumbnails[-1].get("url") if thumbnails else None)
        or f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg"
    )
    return {
        "id": video_id,
        "title": entry.get("title") or video_id,
        "channel": entry.get("channel") or entry.get("uploader") or "",
        "duration": entry.get("duration"),
        "view_count": entry.get("view_count"),
        "thumbnail": thumbnail,
    }


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/search")
def search():
    query = (request.args.get("q") or "").strip()
    if not query:
        return jsonify({"error": "검색어 또는 유튜브 링크를 입력하세요."}), 400

    if is_youtube_url(query):
        target = query
    else:
        target = f"ytsearch{SEARCH_LIMIT}:{query}"

    options = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "skip_download": True,
        "playlist_items": f"1-{SEARCH_LIMIT * 2}",
    }
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(target, download=False)
    except yt_dlp.utils.DownloadError as exc:
        return jsonify({"error": f"유튜브 조회에 실패했습니다: {exc}"}), 502

    if info.get("_type") == "playlist" or "entries" in info:
        items = [entry_to_item(e) for e in (info.get("entries") or []) if e]
    else:
        items = [entry_to_item(info)]

    items = [i for i in items if i["id"]]
    if not items:
        return jsonify({"error": "결과가 없습니다."}), 404
    return jsonify({"items": items})


AUDIO_MIME_BY_EXT = {
    "m4a": "audio/mp4",
    "mp4": "audio/mp4",
    "webm": "audio/webm",
    "opus": "audio/ogg",
    "mp3": "audio/mpeg",
}
PREVIEW_CACHE_TTL = 300  # 초. googlevideo 직링크는 만료되므로 짧게 캐시

_preview_cache: dict[str, dict] = {}
_preview_lock = threading.Lock()


def _get_preview_stream(video_id: str) -> dict:
    """미리듣기용 오디오 직링크를 얻는다. Range 요청마다 재추출하지 않도록 캐시."""
    now = time.time()
    with _preview_lock:
        cached = _preview_cache.get(video_id)
        if cached and cached["expires"] > now:
            return cached

    options = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "format": "bestaudio[ext=m4a]/bestaudio[acodec!=none]/best",
    }
    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(
            f"https://www.youtube.com/watch?v={video_id}", download=False
        )

    stream = {
        "url": info["url"],
        "headers": dict(info.get("http_headers") or {}),
        "mime": AUDIO_MIME_BY_EXT.get(info.get("ext"), "audio/mp4"),
        "expires": now + PREVIEW_CACHE_TTL,
    }
    with _preview_lock:
        _preview_cache[video_id] = stream
    return stream


def _drop_preview_cache(video_id: str) -> None:
    with _preview_lock:
        _preview_cache.pop(video_id, None)


@app.route("/api/preview/<video_id>")
def preview(video_id: str):
    """오디오를 서버 경유로 스트리밍한다. <audio> 탐색을 위해 Range를 그대로 전달."""
    if not VIDEO_ID_RE.fullmatch(video_id):
        abort(400)

    try:
        stream = _get_preview_stream(video_id)
    except Exception as exc:  # noqa: BLE001 - 사용자에게 실패 사유를 전달
        return jsonify({"error": f"미리듣기 준비에 실패했습니다: {exc}"}), 502

    upstream_headers = dict(stream["headers"])
    range_header = request.headers.get("Range")
    if range_header:
        upstream_headers["Range"] = range_header

    try:
        upstream = requests.get(
            stream["url"], headers=upstream_headers, stream=True, timeout=20
        )
    except requests.RequestException as exc:
        _drop_preview_cache(video_id)
        return jsonify({"error": f"스트림 연결에 실패했습니다: {exc}"}), 502

    if upstream.status_code not in (200, 206):
        upstream.close()
        _drop_preview_cache(video_id)  # 직링크 만료(403 등) 시 다음 요청에서 재추출
        return jsonify({"error": "스트림이 만료되었습니다. 다시 시도하세요."}), 502

    response_headers = {"Content-Type": stream["mime"], "Cache-Control": "no-store"}
    for name in ("Content-Length", "Content-Range", "Accept-Ranges"):
        if name in upstream.headers:
            response_headers[name] = upstream.headers[name]

    body = stream_with_context(upstream.iter_content(chunk_size=64 * 1024))
    return Response(body, status=upstream.status_code, headers=response_headers)


@app.route("/api/download/<video_id>")
def download(video_id: str):
    if not VIDEO_ID_RE.fullmatch(video_id):
        abort(400)

    tmpdir = tempfile.mkdtemp(prefix="uptube_")
    options = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "format": "bestaudio/best",
        "outtmpl": os.path.join(tmpdir, "%(id)s.%(ext)s"),
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ],
    }
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(
                f"https://www.youtube.com/watch?v={video_id}", download=True
            )

        title = info.get("title") or video_id
        audio_path = os.path.join(tmpdir, f"{video_id}.mp3")
        if not os.path.exists(audio_path):
            files = os.listdir(tmpdir)
            if not files:
                raise RuntimeError("다운로드된 파일을 찾을 수 없습니다.")
            audio_path = os.path.join(tmpdir, files[0])

        os.makedirs(DOWNLOAD_DIR, exist_ok=True)
        dest = unique_path(DOWNLOAD_DIR, sanitize_filename(title), ".mp3")
        shutil.move(audio_path, dest)
        return jsonify(
            {
                "saved": dest,
                "filename": os.path.basename(dest),
                "folder": DOWNLOAD_DIR,
            }
        )
    except Exception as exc:  # noqa: BLE001 - 사용자에게 실패 사유를 전달
        return jsonify({"error": f"다운로드에 실패했습니다: {exc}"}), 502
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    app.run(host="0.0.0.0", port=port, threaded=True)
