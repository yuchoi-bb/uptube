"""UpTube - YouTube 음원 다운로드 웹앱.

검색어 또는 유튜브 링크를 입력하면 영상 목록을 보여주고,
선택한 영상의 오디오를 mp3로 추출해 영상 제목을 파일명으로 내려준다.
"""

import os
import re
import shutil
import tempfile

from flask import Flask, abort, jsonify, render_template, request, send_file
import yt_dlp

app = Flask(__name__)

YOUTUBE_URL_RE = re.compile(
    r"https?://(www\.|m\.|music\.)?(youtube\.com|youtu\.be)/", re.IGNORECASE
)
VIDEO_ID_RE = re.compile(r"[A-Za-z0-9_-]{5,20}")
UNSAFE_FILENAME_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')

SEARCH_LIMIT = 15


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

        response = send_file(
            audio_path,
            as_attachment=True,
            download_name=sanitize_filename(title) + ".mp3",
            mimetype="audio/mpeg",
        )
        response.call_on_close(lambda: shutil.rmtree(tmpdir, ignore_errors=True))
        return response
    except Exception as exc:  # noqa: BLE001 - 사용자에게 실패 사유를 전달
        shutil.rmtree(tmpdir, ignore_errors=True)
        return jsonify({"error": f"다운로드에 실패했습니다: {exc}"}), 502


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    app.run(host="0.0.0.0", port=port, threaded=True)
