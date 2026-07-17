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
import uuid
from concurrent.futures import ThreadPoolExecutor

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


# ---------------------------------------------------------------------------
# 백그라운드 다운로드 작업 관리
# ---------------------------------------------------------------------------

MAX_CONCURRENT_DOWNLOADS = 3
MAX_FINISHED_JOBS = 50  # 완료/실패 작업은 이만큼만 메모리에 유지

ACTIVE_STATUSES = ("queued", "downloading", "converting")

_executor = ThreadPoolExecutor(max_workers=MAX_CONCURRENT_DOWNLOADS)
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()


def _trim_finished_jobs() -> None:
    """호출 전 _jobs_lock을 잡고 있어야 한다."""
    finished = [j for j in _jobs.values() if j["status"] not in ACTIVE_STATUSES]
    if len(finished) <= MAX_FINISHED_JOBS:
        return
    finished.sort(key=lambda j: j.get("finished_at") or 0)
    for job in finished[: len(finished) - MAX_FINISHED_JOBS]:
        _jobs.pop(job["id"], None)


def _run_download_job(job_id: str) -> None:
    with _jobs_lock:
        job = _jobs.get(job_id)
    if job is None:
        return

    video_id = job["video_id"]
    tmpdir = tempfile.mkdtemp(prefix="uptube_")

    def progress_hook(d: dict) -> None:
        status = d.get("status")
        with _jobs_lock:
            if status == "downloading":
                job["status"] = "downloading"
                job["downloaded_bytes"] = d.get("downloaded_bytes") or 0
                job["total_bytes"] = (
                    d.get("total_bytes") or d.get("total_bytes_estimate")
                )
                job["speed"] = d.get("speed")
            elif status == "finished":
                job["status"] = "converting"
                job["speed"] = None

    options = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "format": "bestaudio/best",
        "outtmpl": os.path.join(tmpdir, "%(id)s.%(ext)s"),
        "progress_hooks": [progress_hook],
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

        title = info.get("title") or job["title"] or video_id
        audio_path = os.path.join(tmpdir, f"{video_id}.mp3")
        if not os.path.exists(audio_path):
            files = os.listdir(tmpdir)
            if not files:
                raise RuntimeError("다운로드된 파일을 찾을 수 없습니다.")
            audio_path = os.path.join(tmpdir, files[0])

        os.makedirs(DOWNLOAD_DIR, exist_ok=True)
        dest = unique_path(DOWNLOAD_DIR, sanitize_filename(title), ".mp3")
        file_size = os.path.getsize(audio_path)
        shutil.move(audio_path, dest)
        with _jobs_lock:
            job.update(
                status="done",
                title=title,
                filename=os.path.basename(dest),
                saved=dest,
                total_bytes=file_size,
                downloaded_bytes=file_size,
                speed=None,
                finished_at=time.time(),
            )
    except Exception as exc:  # noqa: BLE001 - 실패 사유를 작업 상태로 전달
        with _jobs_lock:
            job.update(
                status="error",
                error=str(exc)[:300],
                speed=None,
                finished_at=time.time(),
            )
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


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


@app.route("/api/download/<video_id>", methods=["POST"])
def download(video_id: str):
    """다운로드 작업을 등록하고 즉시 반환한다. 진행 상황은 /api/jobs 로 조회."""
    if not VIDEO_ID_RE.fullmatch(video_id):
        abort(400)

    payload = request.get_json(silent=True) or {}
    title = (payload.get("title") or "").strip() or video_id

    with _jobs_lock:
        for existing in _jobs.values():
            if (
                existing["video_id"] == video_id
                and existing["status"] in ACTIVE_STATUSES
            ):
                return jsonify({"job": dict(existing), "duplicate": True}), 200

        job = {
            "id": uuid.uuid4().hex[:12],
            "video_id": video_id,
            "title": title,
            "status": "queued",
            "downloaded_bytes": 0,
            "total_bytes": None,
            "speed": None,
            "filename": None,
            "saved": None,
            "error": None,
            "created_at": time.time(),
            "finished_at": None,
        }
        _jobs[job["id"]] = job
        _trim_finished_jobs()

    _executor.submit(_run_download_job, job["id"])
    return jsonify({"job": dict(job)}), 202


@app.route("/api/jobs")
def jobs():
    """진행 중/최근 작업 목록과 저장 폴더의 파일 이력."""
    with _jobs_lock:
        job_list = sorted(
            (dict(j) for j in _jobs.values()),
            key=lambda j: j["created_at"],
            reverse=True,
        )

    files = []
    if os.path.isdir(DOWNLOAD_DIR):
        for name in os.listdir(DOWNLOAD_DIR):
            if not name.lower().endswith(".mp3"):
                continue
            path = os.path.join(DOWNLOAD_DIR, name)
            try:
                stat = os.stat(path)
            except OSError:
                continue
            files.append({"name": name, "size": stat.st_size, "mtime": stat.st_mtime})
        files.sort(key=lambda f: f["mtime"], reverse=True)

    active = sum(1 for j in job_list if j["status"] in ACTIVE_STATUSES)
    return jsonify(
        {"jobs": job_list, "files": files, "folder": DOWNLOAD_DIR, "active": active}
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    app.run(host="0.0.0.0", port=port, threaded=True)
