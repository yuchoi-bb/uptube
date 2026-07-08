# UpTube

유튜브 링크나 검색어를 입력하면 영상 목록을 보여주고, 원하는 영상의 음원을
MP3로 다운로드하는 웹앱입니다. 파일명은 해당 유튜브 영상의 제목이 됩니다.

## 요구 사항

- Python 3.10+
- ffmpeg (mp3 변환에 필요)
- tmux (서버 운영용)

```bash
# Ubuntu/Debian
sudo apt install ffmpeg tmux

# Python 패키지
pip install -r requirements.txt
```

## 실행 (tmux)

```bash
./run.sh          # 시작 (기본 포트 8000)
./run.sh stop     # 중지
./run.sh restart  # 재시작
./run.sh status   # 상태 확인
./run.sh logs     # 로그 확인
PORT=9000 ./run.sh  # 다른 포트로 실행
```

브라우저에서 `http://<서버주소>:8000` 접속 후:

1. 유튜브 링크(영상/재생목록) 또는 검색어를 입력
2. 결과 목록에서 원하는 영상의 **MP3 다운로드** 클릭
3. 서버가 음원을 추출·변환한 뒤 `영상제목.mp3` 로 저장됨

## 구조

- `app.py` — Flask 서버. `/api/search` (검색/링크 조회), `/api/download/<id>` (mp3 추출)
- `templates/index.html` — 단일 페이지 UI
- `run.sh` — tmux 세션 관리 스크립트

## 참고

- 음원 추출은 [yt-dlp](https://github.com/yt-dlp/yt-dlp) + ffmpeg 사용 (192kbps mp3)
- 재생목록 링크를 넣으면 앞쪽 30개 항목까지 표시됩니다
- 저작권이 있는 콘텐츠는 개인 소장 등 허용된 범위 내에서만 사용하세요
