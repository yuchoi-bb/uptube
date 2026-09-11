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
    send_file,
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

# 403 Forbidden 등으로 실패하면 다른 player client로 재시도한다.
# 유튜브가 특정 영상(주로 공식 뮤직비디오)에 대해 기본 클라이언트의 스트림을 막는 경우가 있다.
PLAYER_CLIENT_FALLBACKS = ("default", "android_vr", "ios", "tv_simply", "web_safari")
RETRYABLE_ERROR_RE = re.compile(
    r"403|forbidden|unable to download video data|fragment|precondition check failed"
    r"|sign in to confirm|player response|nsig|throttl",
    re.IGNORECASE,
)

ACTIVE_STATUSES = ("queued", "downloading", "converting", "retrying")

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


def available_player_clients() -> tuple[str, ...]:
    """설치된 yt-dlp가 지원하는 클라이언트만 남긴다 (구버전 호환)."""
    try:
        from yt_dlp.extractor.youtube._base import INNERTUBE_CLIENTS
    except Exception:  # noqa: BLE001 - 내부 경로가 없으면 기본 클라이언트만 사용
        return ("default",)
    return tuple(
        c for c in PLAYER_CLIENT_FALLBACKS if c == "default" or c in INNERTUBE_CLIENTS
    )


def _attempt_download(job: dict, tmpdir: str, client: str) -> dict:
    """한 클라이언트로 다운로드를 시도하고 info dict를 반환한다."""

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
    if client != "default":
        options["extractor_args"] = {"youtube": {"player_client": [client]}}

    with yt_dlp.YoutubeDL(options) as ydl:
        return ydl.extract_info(
            f"https://www.youtube.com/watch?v={job['video_id']}", download=True
        )


def _run_download_job(job_id: str) -> None:
    with _jobs_lock:
        job = _jobs.get(job_id)
    if job is None:
        return

    clients = available_player_clients()
    last_error = "알 수 없는 오류"

    for attempt, client in enumerate(clients, start=1):
        tmpdir = tempfile.mkdtemp(prefix="uptube_")
        with _jobs_lock:
            job.update(
                status="queued" if attempt == 1 else "retrying",
                attempt=attempt,
                max_attempts=len(clients),
                client=client,
                downloaded_bytes=0,
                total_bytes=None,
                speed=None,
            )
        try:
            info = _attempt_download(job, tmpdir, client)

            title = info.get("title") or job["title"] or job["video_id"]
            audio_path = os.path.join(tmpdir, f"{job['video_id']}.mp3")
            if not os.path.exists(audio_path):
                files = os.listdir(tmpdir)
                if not files:
                    raise RuntimeError("다운로드된 파일을 찾을 수 없습니다.")
                audio_path = os.path.join(tmpdir, files[0])

            target_dir = os.path.join(DOWNLOAD_DIR, job.get("folder") or "")
            os.makedirs(target_dir, exist_ok=True)
            dest = unique_path(target_dir, sanitize_filename(title), ".mp3")
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
                    error=None,
                    finished_at=time.time(),
                )
            return
        except Exception as exc:  # noqa: BLE001 - 다음 클라이언트로 재시도
            last_error = str(exc)
            if not RETRYABLE_ERROR_RE.search(last_error):
                break  # 영상 삭제/비공개 등은 클라이언트를 바꿔도 소용없다
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    with _jobs_lock:
        job.update(
            status="error",
            error=last_error[:300],
            speed=None,
            finished_at=time.time(),
        )


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

    # 저장할 하위 폴더 (없으면 최상위). date_folder면 오늘 날짜 폴더를 덧붙인다.
    folder = (payload.get("folder") or "").strip().strip("/")
    if folder:
        safe_dir(folder)  # 경로 탈출 검증
    if payload.get("date_folder"):
        today = time.strftime("%Y-%m-%d")
        folder = os.path.join(folder, today) if folder else today

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
            "folder": folder,
            "status": "queued",
            "downloaded_bytes": 0,
            "total_bytes": None,
            "speed": None,
            "filename": None,
            "saved": None,
            "error": None,
            "attempt": 0,
            "max_attempts": len(available_player_clients()),
            "client": None,
            "created_at": time.time(),
            "finished_at": None,
        }
        _jobs[job["id"]] = job
        _trim_finished_jobs()

    _executor.submit(_run_download_job, job["id"])
    return jsonify({"job": dict(job)}), 202


@app.route("/api/jobs/<job_id>/retry", methods=["POST"])
def retry_job(job_id: str):
    """실패한 작업을 같은 큐에 다시 넣는다."""
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job is None:
            return jsonify({"error": "작업을 찾을 수 없습니다."}), 404
        if job["status"] in ACTIVE_STATUSES:
            return jsonify({"job": dict(job), "duplicate": True}), 200
        job.update(
            status="queued",
            error=None,
            downloaded_bytes=0,
            total_bytes=None,
            speed=None,
            attempt=0,
            created_at=time.time(),
            finished_at=None,
        )
        payload = dict(job)

    _executor.submit(_run_download_job, job_id)
    return jsonify({"job": payload}), 202


@app.route("/api/jobs")
def jobs():
    """진행 중/최근 작업 목록과 저장 폴더의 파일 이력."""
    with _jobs_lock:
        job_list = sorted(
            (dict(j) for j in _jobs.values()),
            key=lambda j: j["created_at"],
            reverse=True,
        )

    files = list_dir("")["files"]  # 다운로드 탭에는 최상위 폴더의 파일만
    active = sum(1 for j in job_list if j["status"] in ACTIVE_STATUSES)
    return jsonify(
        {"jobs": job_list, "files": files, "folder": DOWNLOAD_DIR, "active": active}
    )


def safe_dir(rel: str) -> str:
    """저장 폴더 하위 폴더의 절대 경로 (경로 탈출 차단). rel이 비면 최상위."""
    root = os.path.realpath(DOWNLOAD_DIR)
    rel = (rel or "").strip().strip("/")
    if not rel:
        return root
    path = os.path.realpath(os.path.join(root, rel))
    if path != root and not path.startswith(root + os.sep):
        abort(400)
    return path


def safe_entry(rel_path: str, must_be_file: bool = True) -> str:
    """저장 폴더 안의 항목 경로. mp3 파일 또는 하위 폴더만 허용한다."""
    rel_path = (rel_path or "").strip().strip("/")
    if not rel_path:
        abort(400)
    parent = safe_dir(os.path.dirname(rel_path))
    name = os.path.basename(rel_path)
    if not name or name in (".", "..") or name.startswith("."):
        abort(400)
    path = os.path.realpath(os.path.join(parent, name))
    root = os.path.realpath(DOWNLOAD_DIR)
    if not path.startswith(root + os.sep):
        abort(400)
    if must_be_file:
        if not path.lower().endswith(".mp3"):
            abort(400)
        if not os.path.isfile(path):
            abort(404)
    return path


def list_dir(rel: str) -> dict:
    """폴더 하나의 내용 (하위 폴더 + mp3 목록)."""
    base = safe_dir(rel)
    dirs, files = [], []
    if os.path.isdir(base):
        for name in os.listdir(base):
            if name.startswith("."):
                continue
            path = os.path.join(base, name)
            try:
                stat = os.stat(path)
            except OSError:
                continue
            if os.path.isdir(path):
                try:
                    inner = os.listdir(path)
                except OSError:
                    inner = []
                dirs.append(
                    {
                        "name": name,
                        "mtime": stat.st_mtime,
                        "count": sum(1 for n in inner if n.lower().endswith(".mp3")),
                    }
                )
            elif name.lower().endswith(".mp3"):
                files.append(
                    {"name": name, "size": stat.st_size, "mtime": stat.st_mtime}
                )
    dirs.sort(key=lambda d: d["name"].lower())
    files.sort(key=lambda f: f["mtime"], reverse=True)
    return {"dirs": dirs, "files": files}


def all_folders() -> list[str]:
    """저장 폴더 아래 모든 하위 폴더의 상대 경로 (저장 위치 선택용)."""
    root = os.path.realpath(DOWNLOAD_DIR)
    found = []
    if os.path.isdir(root):
        for base, subdirs, _ in os.walk(root):
            subdirs[:] = sorted(d for d in subdirs if not d.startswith("."))
            if base != root:
                found.append(os.path.relpath(base, root))
    return sorted(found)


# 폴더 이름으로 쓸 수 없는 문자
FOLDER_NAME_RE = re.compile(r'^[^\\/:*?"<>|\x00-\x1f]{1,60}$')


@app.route("/api/files")
def files_list():
    """파일 탐색기용 목록. ?path=<하위 폴더 상대 경로>"""
    rel = (request.args.get("path") or "").strip().strip("/")
    safe_dir(rel)  # 경로 검증
    content = list_dir(rel)
    return jsonify(
        {
            "root": DOWNLOAD_DIR,
            "path": rel,
            "folder": os.path.join(DOWNLOAD_DIR, rel) if rel else DOWNLOAD_DIR,
            "dirs": content["dirs"],
            "files": content["files"],
            "count": len(content["files"]),
            "total_size": sum(f["size"] for f in content["files"]),
            "all_folders": all_folders(),
        }
    )


@app.route("/api/file")
def file_get():
    """재생용 스트리밍(기본) 또는 보고 있는 기기로 저장(?download=1). ?path=상대경로"""
    rel = request.args.get("path") or ""
    path = safe_entry(rel)
    return send_file(
        path,
        mimetype="audio/mpeg",
        as_attachment=request.args.get("download") == "1",
        download_name=os.path.basename(path),
        conditional=True,  # Range 지원 → 재생 바 탐색 가능
    )


@app.route("/api/file", methods=["DELETE"])
def file_delete():
    path = safe_entry(request.args.get("path") or "")
    try:
        os.remove(path)
    except OSError as exc:
        return jsonify({"error": f"삭제에 실패했습니다: {exc}"}), 500
    return jsonify({"deleted": os.path.basename(path)})


@app.route("/api/file/move", methods=["POST"])
def file_move():
    """파일을 다른 폴더로 옮긴다."""
    payload = request.get_json(silent=True) or {}
    src = safe_entry(payload.get("from") or "")
    dest_dir = safe_dir(payload.get("to") or "")
    if not os.path.isdir(dest_dir):
        return jsonify({"error": "대상 폴더가 없습니다."}), 404
    name = os.path.basename(src)
    if os.path.dirname(src) == dest_dir:
        return jsonify({"moved": name, "unchanged": True})
    stem, ext = os.path.splitext(name)
    dest = unique_path(dest_dir, stem, ext)
    try:
        shutil.move(src, dest)
    except OSError as exc:
        return jsonify({"error": f"이동에 실패했습니다: {exc}"}), 500
    return jsonify({"moved": os.path.basename(dest)})


@app.route("/api/folder", methods=["POST"])
def folder_create():
    """하위 폴더를 만든다. {path: 부모 상대경로, name: 새 폴더 이름}"""
    payload = request.get_json(silent=True) or {}
    name = (payload.get("name") or "").strip()
    if not FOLDER_NAME_RE.match(name) or name in (".", ".."):
        return jsonify({"error": "폴더 이름에 쓸 수 없는 문자가 있습니다."}), 400
    parent = safe_dir(payload.get("path") or "")
    target = os.path.join(parent, name)
    if os.path.exists(target):
        return jsonify({"error": "같은 이름의 폴더가 이미 있습니다."}), 409
    try:
        os.makedirs(target)
    except OSError as exc:
        return jsonify({"error": f"폴더 생성에 실패했습니다: {exc}"}), 500
    return jsonify({"created": name}), 201


@app.route("/api/folder", methods=["DELETE"])
def folder_delete():
    """빈 폴더만 삭제한다 (안에 파일이 있으면 거부)."""
    rel = (request.args.get("path") or "").strip().strip("/")
    if not rel:
        return jsonify({"error": "최상위 폴더는 삭제할 수 없습니다."}), 400
    path = safe_dir(rel)
    if not os.path.isdir(path):
        return jsonify({"error": "폴더를 찾을 수 없습니다."}), 404
    if os.listdir(path):
        return jsonify({"error": "폴더가 비어 있지 않습니다."}), 409
    try:
        os.rmdir(path)
    except OSError as exc:
        return jsonify({"error": f"삭제에 실패했습니다: {exc}"}), 500
    return jsonify({"deleted": rel})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    app.run(host="0.0.0.0", port=port, threaded=True)
